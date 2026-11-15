"""우리끼리 TRIP · 설정 없이 시작하는 가족용 Streamlit 앱
배포: app.py와 requirements.txt를 GitHub에 업로드하고 Streamlit Community Cloud에서
app.py를 선택해 배포하면 끝. 토큰, Secrets, 외부 데이터베이스 설정 불필요.
첫 접속부터 홈을 바로 표시. 기본 가족은 엄마·아빠·나·동생이며 운영 > 가족 설정에서 변경.
같은 앱 주소를 쓰는 모든 사람이 함께 사용한다.
보안: 로그인 없는 공용 가족 공간이다. 앱 주소를 가족끼리만 공유하고 민감정보 입력 금지.
저장: 서버의 SQLite 파일 사용. 같은 서버의 동시 사용자에게 공유되고 새로고침 후 유지.
Community Cloud 재부팅/절전 복귀/재배포/서버 교체 때 영구 보존은 보장되지 않는다.
여러 서버 복제본 간 동기화는 지원하지 않는다. 개인별 인증/자동 push 동기화 없음.
스마트폰 기본 글자 크기에 맞춘 컴팩트 디자인이며 아주 작은 화면에서는 스크롤 허용.
"""
from __future__ import annotations

import base64
import calendar
import copy
import hashlib
import hmac
import html
import io
import json
import secrets
from datetime import date, datetime, timedelta, timezone

import qrcode
import sqlite3
from pathlib import Path
import streamlit as st

st.set_page_config(page_title="우리끼리 · 가족 여행 뽑기", page_icon="🎒", layout="centered", initial_sidebar_state="collapsed")
KST = timezone(timedelta(hours=9))
KINDS = {"소모임": (1, "☕", "외식 · 피크닉"), "중모임": (2, "🚗", "당일치기 여행"), "대모임": (6, "🏡", "숙박 여행")}


def today():
    return datetime.now(KST).date()


def add_months(d, months):
    y, m = divmod(d.year * 12 + d.month - 1 + months, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def candidates(submissions, members, nights, floor):
    """모든 구성원이 제출해야 확정. 숙박은 체크아웃일까지 연속 날짜 필요."""
    if not all(name in submissions for name in members):
        return []
    common = set.intersection(*(set(submissions[name]["dates"]) for name in members))
    result = []
    for value in sorted(common):
        start = date.fromisoformat(value)
        if start >= floor and all((start + timedelta(days=i)).isoformat() in common for i in range(nights + 1)):
            result.append(value)
    return result


def digest_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 200_000).hex()


def verify_admin(room, pin):
    # 별도 PIN 없이 사용하는 신뢰 기반 가족 공용 공간입니다.
    return True


def tokens(value):
    return list(dict.fromkeys(v.strip()[:45] for v in value.replace("，", ",").split(",") if v.strip()))[:3]


def escaped(value):
    return html.escape(str(value))


class StoreError(Exception):
    pass


class RuleError(Exception):
    pass


class LocalStore:
    """동일 서버 접속자들이 공유하는 트랜잭션 기반 SQLite 저장소."""
    def __init__(self):
        self.path = str(Path(__file__).resolve().parent / "family_trip.sqlite3")
        try:
            with sqlite3.connect(self.path, timeout=20) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS family (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
        except sqlite3.Error as exc:
            raise StoreError("가족 저장 공간을 열 수 없습니다. 잠시 후 다시 시도하세요.") from exc

    def load(self):
        try:
            with sqlite3.connect(self.path, timeout=20) as conn:
                row = conn.execute("SELECT data FROM family WHERE id=1").fetchone()
            return (json.loads(row[0]) if row else None), None
        except (sqlite3.Error, ValueError) as exc:
            raise StoreError("가족 데이터를 읽을 수 없습니다. 잠시 후 다시 시도하세요.") from exc

    def change(self, operation):
        conn = sqlite3.connect(self.path, timeout=20)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT data FROM family WHERE id=1").fetchone()
            room = json.loads(row[0]) if row else None
            updated = operation(copy.deepcopy(room))
            conn.execute("INSERT INTO family(id,data) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                         (json.dumps(updated, ensure_ascii=False),))
            conn.commit()
            return updated
        except sqlite3.Error as exc:
            conn.rollback()
            raise StoreError("저장하지 못했습니다. 새로고침 후 다시 시도하세요.") from exc
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def active(room, round_id):
    if room is None or room["round"]["id"] != round_id:
        raise RuleError("회차가 바뀌었습니다. 새로고침 후 다시 입력해 주세요.")
    return room["round"]


def new_round(kind, nights, start, days=21):
    return {"id": secrets.token_hex(8), "kind": kind, "nights": nights,
            "window_start": start.isoformat(), "window_end": (start + timedelta(days=days-1)).isoformat(),
            "submissions": {}, "confirmed": None, "draw": None, "completed": False}


def save_submission(room, round_id, member, entry, floor):
    r = active(room, round_id)
    if r["confirmed"] or r["completed"]:
        raise RuleError("이미 확정된 회차입니다. 날짜와 취향을 변경할 수 없습니다.")
    if member not in room["members"]:
        raise RuleError("가족 구성원을 확인해 주세요.")
    if any(not (r["window_start"] <= d <= r["window_end"]) or date.fromisoformat(d) < floor for d in entry["dates"]):
        raise RuleError("지난 날짜 또는 모집 범위 밖 날짜가 있습니다.")
    r["submissions"][member] = entry
    options = candidates(r["submissions"], room["members"], r["nights"], floor)
    if options:
        r["confirmed"] = options[0]
    return room


def draw_options(room, round_id, pin):
    r = active(room, round_id)
    if not verify_admin(room, pin):
        raise RuleError("운영 PIN이 맞지 않습니다.")
    if not r["confirmed"]:
        raise RuleError("전원 제출과 날짜 확정 후 추첨해 주세요.")
    if r["draw"]:
        raise RuleError("이미 추첨했습니다. 재추첨은 불가능합니다.")
    result = {}
    for field in ("concept", "place", "food"):
        pool = sorted({v for entry in r["submissions"].values() for v in entry[field]})
        if not pool:
            raise RuleError("컨셉·장소·음식 후보가 모두 필요합니다.")
        result[field] = {"winner": secrets.choice(pool), "count": len(pool), "pool": pool}
    r["draw"] = result
    return room


st.markdown("""<style>
:root {color-scheme:light;}
.stApp {background:#f5f4ef;color:#182a23;}
header[data-testid="stHeader"] {background:transparent;height:0;}
.block-container {max-width:460px;padding:0.7rem 0.85rem 1rem;}
[data-testid="stToolbar"], #MainMenu, footer {display:none;}
h1,h2,h3,p {margin:0;} .brand {font-size:23px;font-weight:850;letter-spacing:-1px;}
.sub {font-size:12px;color:#56675e;margin:2px 0 9px;}
.hero {background:#173e32;color:#fff;border-radius:20px;padding:15px 18px;margin:8px 0;}
.hero small {color:#d9e9df;} .big {font-size:35px;font-weight:850;line-height:1.2;}
.hero .line {font-size:13px;margin-top:5px;color:#e8f2ec;}
.card {background:white;border:1px solid #e1e5dd;border-radius:16px;padding:12px 14px;margin:7px 0;}
.card b {font-size:15px;} .card small {font-size:12px;color:#526259;}
.triplet {display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin:9px 0;}
.cool {background:#fff;border:1px solid #e1e5dd;border-radius:14px;text-align:center;padding:10px 2px;font-size:12px;}
.cool strong {display:block;font-size:17px;color:#1e6148;margin:3px 0;}
.chips {font-size:12px;line-height:1.7;color:#405a4a;}
.rule {font-size:11px;color:#5d6c63;line-height:1.5;margin:7px 0;}
.stButton button,.stFormSubmitButton button,.stLinkButton a {border-radius:12px;min-height:38px;font-weight:650;}
.stButton button[kind="primary"],.stFormSubmitButton button[kind="primary"] {background:#23644d;border:0;}
[data-testid="stVerticalBlock"] {gap:0.4rem;}
[data-testid="stWidgetLabel"] p {font-size:12px;}
[data-testid="stForm"] {border:0;padding:0;}
.stTextInput input {min-height:37px;} [data-testid="stCaptionContainer"] {font-size:11px;}
[data-testid="stAlert"] {padding:0.5rem;font-size:12px;}
[data-testid="stPills"] button {padding:5px 10px;font-size:12px;}
@media (max-width:380px) {.block-container{padding-left:0.6rem;padding-right:0.6rem;}.hero{padding:11px 14px;}.big{font-size:29px;}}
</style>""", unsafe_allow_html=True)
st.markdown('<div class="brand">🎒 우리끼리 <span style="color:#438360">TRIP</span></div><div class="sub">날짜는 함께 맞추고, 취향은 공정하게 뽑아요.</div>', unsafe_allow_html=True)

try:
    store = LocalStore()
    room, _ = store.load()
except StoreError as exc:
    st.error(str(exc))
    st.stop()

if room is None:
    # 첫 방문도 시작/설정 화면 없이 곧바로 홈으로 진입한다.
    def initialize(old):
        if old is not None:
            return old
        return {"version": 3, "members": ["엄마", "아빠", "나", "동생"],
                "last": {}, "history": [],
                "round": new_round("소모임", 0, today())}
    try:
        room = store.change(initialize)
    except StoreError as exc:
        st.error(str(exc))
        st.stop()

r = room["round"]
round_id = r["id"]
now = today()
nav = st.pills("메뉴", ["🏠 홈", "🗓 입력", "🎲 뽑기", "⚙️ 운영", "📱 QR"], default="🏠 홈", label_visibility="collapsed", key="nav")


def mutate(operation, message):
    try:
        store.change(operation)
        st.session_state.flash = message
        st.rerun()
    except (StoreError, RuleError) as exc:
        st.error(str(exc))


if st.session_state.get("flash"):
    st.success(st.session_state.pop("flash"))

if nav == "🏠 홈":
    if r["confirmed"]:
        start = date.fromisoformat(r["confirmed"])
        end = start + timedelta(days=r["nights"])
        diff = (start - now).days
        dday = f"D-{diff}" if diff > 0 else ("D-DAY" if diff == 0 else f"D+{abs(diff)}")
        label = "완료한 만남" if r["completed"] else ("진행일이 지났어요 · 운영에서 완료 처리" if now > end else "우리 가족의 약속 · 자동 확정")
        text = f'{start:%m.%d} ~ {end:%m.%d}' if r["nights"] else f'{start:%m.%d} ({["월","화","수","목","금","토","일"][start.weekday()]})'
    else:
        dday = f'{len(r["submissions"])}/{len(room["members"])}명'
        label = "가능한 날짜를 모으고 있어요"
        text = "전원 제출 → 가장 빠른 공통 날짜 자동 확정"
    st.markdown(f'<div class="hero"><small>{label}</small><div class="big">{dday}</div><div class="line">{escaped(r["kind"])} · {text}</div></div>', unsafe_allow_html=True)
    cards = []
    for kind, (months, icon, _) in KINDS.items():
        last = room["last"].get(kind)
        if last:
            due = add_months(date.fromisoformat(last), months)
            left = max(0, (due - now).days)
            status = "지금 만나요" if left == 0 else f"약 {left/30.44:.1f}개월"
            foot = f"{due:%m.%d} · {left}일 남음"
        else:
            status, foot = "첫 만남 대기", "완료 이력 없음"
        cards.append(f'<div class="cool">{icon} {kind}<strong>{status}</strong>{foot}</div>')
    st.markdown('<div class="triplet">'+''.join(cards)+'</div>', unsafe_allow_html=True)
    if r["draw"]:
        d = r["draw"]
        st.markdown(f'<div class="card"><b>📍 {escaped(d["place"]["winner"])}</b><div class="chips">✨ {escaped(d["concept"]["winner"])} &nbsp; 🍴 {escaped(d["food"]["winner"])}</div></div>', unsafe_allow_html=True)
    else:
        pending = [name for name in room["members"] if name not in r["submissions"]]
        line = "미제출: " + ", ".join(pending) if pending else ("날짜 확정! 뽑기에서 취향을 결정해요." if r["confirmed"] else "공통 날짜 없음 · 입력에서 날짜를 조정해요.")
        st.markdown(f'<div class="card"><b>{KINDS[r["kind"]][1]} {KINDS[r["kind"]][2]}</b><div class="chips">{escaped(line)}</div></div>', unsafe_allow_html=True)
    st.markdown('<div class="rule">처음이라면 운영 → 가족 설정에서 이름을 바꿔 주세요.<br>약속 원칙: 전원 가능한 날짜가 생기면 자동 확정, 이후 날짜 변경·재추첨 불가.<br>주기: 소 1개월 · 중 2개월 · 대 6개월 / 마지막 실제 완료일부터 계산</div>', unsafe_allow_html=True)
    if st.button("↻ 가족 입력 새로고침", use_container_width=True):
        st.rerun()

elif nav == "🗓 입력":
    if r["confirmed"]:
        st.success(f'날짜 확정: {r["confirmed"]} · 입력이 잠겼어요.')
        st.caption("일정은 앱 내에서 고정됩니다. 실제 참여를 강제할 수는 없으며 긴급 상황은 가족끼리 상의하세요.")
    else:
        member = st.selectbox("나는 누구인가요?", room["members"])
        prior = r["submissions"].get(member, {})
        begin = max(now, date.fromisoformat(r["window_start"]))
        finish = date.fromisoformat(r["window_end"])
        choices = [(begin + timedelta(days=i)).isoformat() for i in range(max(0, (finish-begin).days+1))]
        st.caption(f'{r["kind"]} · {r["window_start"][5:]}~{r["window_end"][5:]} · '+(f'{r["nights"]}박, 체크아웃일까지 모두 선택' if r["nights"] else '가능한 날짜를 모두 선택'))
        if not choices:
            st.warning("모집 기간이 지났습니다. 운영에서 새 회차를 열어 주세요.")
        else:
            with st.form(f"entry_{round_id}_{member}"):
                dates = st.multiselect("가능한 날짜", choices, default=[d for d in prior.get("dates", []) if d in choices], format_func=lambda d: date.fromisoformat(d).strftime("%m/%d")+" "+["월","화","수","목","금","토","일"][date.fromisoformat(d).weekday()], placeholder="날짜 선택 (복수 가능)")
                concept = st.text_input("✨ 컨셉 (쉼표로 최대 3개)", value=", ".join(prior.get("concept", [])), placeholder="힐링, 자연", max_chars=140)
                place = st.text_input("📍 장소 · 지역", value=", ".join(prior.get("place", [])), placeholder="서울숲, 남이섬", max_chars=140)
                food = st.text_input("🍴 먹고 싶은 음식", value=", ".join(prior.get("food", [])), placeholder="파스타, 바비큐", max_chars=140)
                if st.form_submit_button("내 일정 · 취향 저장", type="primary", use_container_width=True):
                    entry = {"dates": sorted(dates), "concept": tokens(concept), "place": tokens(place), "food": tokens(food)}
                    if not all(entry[field] for field in ("concept", "place", "food")):
                        st.error("컨셉·장소·음식을 각각 하나 이상 입력해 주세요.")
                    else:
                        mutate(lambda doc: save_submission(doc, round_id, member, entry, today()), "저장했어요! 전원 교집합이 있으면 자동 확정됩니다.")
        st.caption("가능한 날이 없으면 날짜를 비워 제출하세요. 교집합이 없으면 자동 확정하지 않으며 각자 다시 수정할 수 있어요.")

elif nav == "🎲 뽑기":
    st.markdown('<div class="hero"><small>한 번만 뽑는 가족 복불복</small><div class="big">LUCKY TRIP 🎲</div><div class="line">중복 후보는 합치고, 후보마다 같은 확률!</div></div>', unsafe_allow_html=True)
    if r["draw"]:
        for field, label in [("concept", "✨ 여행 컨셉"), ("place", "📍 최종 목적지"), ("food", "🍴 최종 메뉴")]:
            chosen = r["draw"][field]
            st.markdown(f'<div class="card"><small>{label} · {chosen["count"]}개 중 선택</small><br><b>{escaped(chosen["winner"])}</b></div>', unsafe_allow_html=True)
        st.caption("결과는 가족 공용으로 저장됩니다. 같은 회차는 재추첨할 수 없어요. 예약·날씨·알레르기는 사전에 꼭 확인하세요.")
    elif not r["confirmed"]:
        st.info("전원 일정 제출 → 날짜 자동 확정 후 추첨할 수 있어요.")
    else:
        st.caption("각자 최대 3개 제안 · 동일 후보는 1개로 통합 · 가족 대표가 1회 추첨. 사람별이 아닌 고유 후보별 동일 확률입니다.")
        for field, label in [("concept", "컨셉"), ("place", "장소"), ("food", "음식")]:
            pool = sorted({x for item in r["submissions"].values() for x in item[field]})
            st.markdown(f'<div class="chips"><b>{label}</b> · {escaped(" / ".join(pool))}</div>', unsafe_allow_html=True)
        with st.form("draw"):
            pin = ""
            if st.form_submit_button("🎲 공정하게 뽑기", type="primary", use_container_width=True):
                mutate(lambda doc: draw_options(doc, round_id, pin), "추첨 완료! 가족의 여행 취향이 결정됐어요.")

elif nav == "⚙️ 운영":
    action = st.pills("운영 메뉴", ["다음 회차", "모임 완료", "가족 설정", "기록"], default="다음 회차", label_visibility="collapsed")
    if action == "가족 설정":
        st.caption("확정 전 또는 완료 후에만 가족을 변경할 수 있어요. 변경하면 현재 회차의 미확정 입력이 초기화됩니다.")
        with st.form("members"):
            names = st.text_input("가족 별명 · 쉼표 구분", value=", ".join(room["members"]), max_chars=140)
            agree = st.checkbox("현재 미확정 입력 초기화에 동의")
            if st.form_submit_button("가족 변경", use_container_width=True):
                members = list(dict.fromkeys(n.strip() for n in names.split(",") if n.strip()))
                def change_members(doc):
                    current = active(doc, round_id)
                    if not agree or not 2 <= len(members) <= 8 or any(len(n)>12 for n in members):
                        raise RuleError("초기화 동의, 가족 2~8명, 별명 12자 이하를 확인하세요.")
                    if current["confirmed"] and not current["completed"]:
                        raise RuleError("확정된 모임을 완료한 후 가족을 변경하세요.")
                    doc["members"] = members
                    if not current["completed"]:
                        doc["round"] = new_round(current["kind"], current["nights"], today())
                    return doc
                mutate(change_members, "가족 구성을 변경했어요.")
    elif action == "기록":
        logs = room["history"]
        if not logs:
            st.info("아직 완료한 모임이 없어요.")
        else:
            log_choices = list(reversed(range(len(logs))))
            index = st.selectbox("완료 기록", log_choices, format_func=lambda i: f'{logs[i]["confirmed"]} · {logs[i]["kind"]}')
            log = logs[index]
            result = log.get("draw") or {}
            st.markdown(f'<div class="card"><b>{escaped(log["kind"])} · {escaped(log["confirmed"])}</b><div class="chips">📍 {escaped(result.get("place", {}).get("winner", "미추첨"))}<br>🍴 {escaped(result.get("food", {}).get("winner", "미추첨"))}</div></div>', unsafe_allow_html=True)
    elif action == "모임 완료":
        st.caption("여행 마지막 날부터 완료 처리 가능. 실제 완료일로 주기를 계산하고 기록에 보관합니다.")
        with st.form("complete"):
            done_day = st.date_input("실제 완료일", value=now, max_value=now)
            pin = ""
            if st.form_submit_button("✅ 함께 다녀왔어요", type="primary", use_container_width=True):
                def complete(doc):
                    current = active(doc, round_id)
                    if not verify_admin(doc, pin):
                        raise RuleError("운영 PIN이 맞지 않습니다.")
                    if not current["confirmed"] or current["completed"]:
                        raise RuleError("확정 전이거나 이미 완료한 회차입니다.")
                    end_day = date.fromisoformat(current["confirmed"]) + timedelta(days=current["nights"])
                    if not end_day <= done_day <= today():
                        raise RuleError("예정된 마지막 날 이후, 오늘까지의 날짜만 가능합니다.")
                    current["completed"] = True
                    doc["last"][current["kind"]] = done_day.isoformat()
                    doc["history"].append(copy.deepcopy(current))
                    return doc
                mutate(complete, "모임 완료! 다음 모임 쿨타임이 시작됐어요.")
    else:
        st.caption("확정된 회차는 완료해야 다음 회차를 열 수 있어요. 미확정 회차는 새로 모집하면 기존 입력이 초기화됩니다.")
        with st.form("next"):
            c1, c2 = st.columns(2)
            kind = c1.selectbox("모임 규모", list(KINDS))
            nights = c2.selectbox("박수 (대모임만)", [1, 2, 3])
            start = st.date_input("모집 시작일 · 이후 21일 중 선택", value=now, min_value=now, max_value=now+timedelta(days=365))
            pin = ""
            agree = st.checkbox("미확정 입력 초기화 또는 완료 후 새 모집에 동의")
            if st.form_submit_button("새 회차 열기", type="primary", use_container_width=True):
                def restart(doc):
                    current = active(doc, round_id)
                    if not verify_admin(doc, pin):
                        raise RuleError("운영 PIN이 맞지 않습니다.")
                    if not agree:
                        raise RuleError("초기화 동의를 체크해 주세요.")
                    if current["confirmed"] and not current["completed"]:
                        raise RuleError("확정된 일정은 변경 불가. 모임을 완료한 뒤 새로 열어 주세요.")
                    doc["round"] = new_round(kind, nights if kind == "대모임" else 0, start)
                    return doc
                mutate(restart, "새 회차를 열었어요. 입력에서 날짜와 취향을 모아 주세요.")
        st.caption("주기는 권장 간격이며, 쿨타임 중에도 새 모임은 모집할 수 있습니다.")

elif nav == "📱 QR":
    st.markdown('<div class="card"><b>📱 가족에게 앱 공유</b><div class="chips">같은 주소로 접속하면 바로 함께 사용해요.<br>가족방 코드나 비밀번호는 필요 없어요.</div></div>', unsafe_allow_html=True)
    app_url = st.text_input("현재 앱 주소를 붙여넣으세요", placeholder="https://내앱.streamlit.app", key="share_url").strip()
    if app_url.startswith("https://"):
        from urllib.parse import urlsplit
        parsed = urlsplit(app_url)
        if not parsed.hostname or parsed.username or parsed.password:
            st.warning("올바른 https 앱 주소를 입력해 주세요.")
        else:
            image = qrcode.make(app_url)
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            st.image(stream.getvalue(), width=170)
            st.download_button("QR 이미지 내려받기", stream.getvalue(), "family-trip-qr.png", "image/png", use_container_width=True)
    elif app_url:
        st.warning("https://로 시작하는 앱 주소를 넣어 주세요.")
    else:
        st.caption("주소창의 앱 주소만 복사해 넣으면 QR이 나옵니다. 앱 사용 자체에는 필요하지 않아요.")
    st.caption("주소를 아는 누구나 내용을 보고 수정할 수 있어요. 가족끼리만 공유하고 개인정보는 적지 마세요.")
    st.caption("서버 재시작·재배포 시 저장 기록이 사라질 수 있습니다.")

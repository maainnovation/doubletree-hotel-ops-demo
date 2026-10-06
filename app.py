"""Hotel Operations Copilot: a mobile-first Streamlit demonstration."""
from pathlib import Path
import os
import uuid
from copy import deepcopy
import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError
from dotenv import load_dotenv

from services.data_loader import load_demo_data
from engine.validation import validate_data
from services.operations import calculate_components
from services.approval import approve, invalidate
from services.audit import record_change
from services.state_store import StateStore
from copilot.llm import answer_question
from copilot.parameter_chat import parse_change, resolve_change, change_reply
from services.operations import build_snapshot
from copilot.responses import housekeeping_response
from copilot.scenarios import initial_scenarios

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
DATA_PATH = Path(os.getenv("HOTEL_OPS_WORKBOOK", ROOT / "data" / "Hotel_Ops_Demo_Data.xlsx"))
WEB_MANAGER_ID = os.getenv("WEB_MANAGER_ID", "demo-gm")
state_store = StateStore()

st.set_page_config(page_title="My Copilot · Hotel operations", page_icon=":material/concierge:", layout="centered")

# Streamlit Cloud secrets are injected into the environment for existing service
# adapters, while local `.env` remains the development source of truth.
try:
    for secret_name in ("OPENAI_API_KEY", "OPENAI_MODEL"):
        if not os.getenv(secret_name) and secret_name in st.secrets:
            os.environ[secret_name] = str(st.secrets[secret_name])
except StreamlitSecretNotFoundError:
    pass

# Local demos keep the configured manager record. Public cloud visitors receive
# a separate anonymous state record for each Streamlit browser session.
WEB_MANAGER_ID = os.getenv("WEB_MANAGER_ID") or st.session_state.setdefault("web_manager_id", uuid.uuid4().hex)

persisted_state = state_store.get_manager(WEB_MANAGER_ID)
if "scenarios" not in st.session_state: st.session_state.scenarios = persisted_state["scenarios"]
if "approval" not in st.session_state: st.session_state.approval = persisted_state["approval"]
if "pending" not in st.session_state: st.session_state.pending = persisted_state["pending"]
if "audit" not in st.session_state: st.session_state.audit = []
if "chat" not in st.session_state: st.session_state.chat = []

PARAMETER_DEFAULTS = {
    "checkout_minutes": 22, "stayover_minutes": 10, "inspection_minutes": 3,
    "paid_shift_hours": 8, "break_minutes": 60, "other_duties_minutes": 30,
    "late_release_minutes": 0, "attendant_adjustment": 0,
    "prior_room_nights": 4500, "forecast_room_nights": 7200, "forecast_days": 30,
    "safety_days": 7, "budget_usd": 8000,
}

def workbook_parameters(settings):
    values = {}
    for key, fallback in PARAMETER_DEFAULTS.items():
        value = settings.get(key, fallback)
        values[key] = float(value) if key in {"paid_shift_hours", "budget_usd"} else int(float(value))
    return values

def persist_web_state(pending=None):
    if pending and not pending.get("expires_at"):
        pending["expires_at"] = __import__("time").time() + 15 * 60
    state_store.save_manager(WEB_MANAGER_ID, {
        "scenarios": st.session_state.scenarios,
        "pending": pending,
        "approval": st.session_state.approval,
    })

def record_web_change(field, old_value, new_value, reason="Scenario applied"):
    record_change(st.session_state.audit, field, old_value, new_value, "GM Scenario", reason)
    state_store.record_audit(WEB_MANAGER_ID, st.session_state.audit[-1])

def apply_parameter_values(values):
    previous = dict(st.session_state.parameters)
    if previous["checkout_minutes"] != values["checkout_minutes"]:
        st.session_state.scenarios["checkout_minutes"] = None
    if previous["budget_usd"] != values["budget_usd"]:
        st.session_state.scenarios["budget_usd"] = None
    st.session_state.parameters = values
    if previous != values:
        invalidate(st.session_state.approval, "Operating parameters changed")
        record_web_change("operating_parameters", previous, values, "Manager updated calculation parameters")
        persist_web_state(st.session_state.pending)

def handle_chat_change(message):
    request = parse_change(message)
    if request is None:
        return None
    if "message" in request:
        return request["message"]
    before = build_snapshot(st.session_state.scenarios, path=DATA_PATH, parameters=st.session_state.parameters)
    try:
        updates = resolve_change(request, before["parameters"])
        parameters = {**st.session_state.parameters, **updates}
        scenarios = deepcopy(st.session_state.scenarios)
        for key in updates:
            if key in {"checkout_minutes", "budget_usd"}:
                scenarios[key] = None
            elif key == "late_release_minutes":
                scenarios[key] = 0
        after = build_snapshot(scenarios, path=DATA_PATH, parameters=parameters)
    except ValueError as exc:
        return f"**No change applied.** {exc}"
    if not request["preview"]:
        previous = {"parameters": dict(st.session_state.parameters), "scenarios": deepcopy(st.session_state.scenarios)}
        st.session_state.scenarios = scenarios
        apply_parameter_values(parameters)
        for name, value in parameters.items():
            st.session_state[f"param_{name}"] = value
        st.session_state.chat_undo = {"before": previous, "after_parameters": dict(parameters), "after_scenarios": deepcopy(scenarios)}
    return change_reply(before, after, updates, request["preview"])


def undo_chat_change():
    saved = st.session_state.get("chat_undo")
    if not saved:
        return
    if st.session_state.parameters != saved["after_parameters"] or st.session_state.scenarios != saved["after_scenarios"]:
        st.warning("The plan has changed since that chat edit. Adjust the current values in Parameters.")
        return
    apply_parameter_values(saved["before"]["parameters"])
    st.session_state.scenarios = saved["before"]["scenarios"]
    for name, value in st.session_state.parameters.items():
        st.session_state[f"param_{name}"] = value
    persist_web_state(st.session_state.pending)
    st.session_state.chat_undo = None
    st.session_state.chat.append(("assistant", "**Change undone.** Your previous session parameters and calculations have been restored."))


def go_to(page):
    st.session_state.navigation = page


def queue_question(question):
    st.session_state.navigation = "AI Chat - My Copilot"
    st.session_state.queued_question = question


def metric_card(column, label, value, detail, icon):
    with column.container(border=True):
        st.caption(f"{icon} {label}")
        st.metric(label, value, label_visibility="collapsed")
        st.caption(detail)


def reset_parameter_values():
    defaults = workbook_parameters(data.settings)
    apply_parameter_values(defaults)
    for name, value in defaults.items():
        st.session_state[f"param_{name}"] = value

def load_state():
    try:
        data = load_demo_data(DATA_PATH)
        return data, validate_data(data), None
    except Exception as exc:
        return None, None, str(exc)

data, validation, load_error = load_state()
if data and "parameters" not in st.session_state:
    st.session_state.parameters = workbook_parameters(data.settings)
with st.container(horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"):
    st.markdown(":material/concierge: **HOTEL OPERATIONS**")
    with st.popover("Workspace", icon=":material/tune:"):
        st.caption("Your demo workspace")
        if st.button("Refresh workbook", icon=":material/refresh:", width="stretch"):
            data, validation, load_error = load_state()
            if validation and validation.is_valid:
                st.toast("Hotel data refreshed", icon=":material/check_circle:")
        if st.button("Reset demo", icon=":material/restart_alt:", width="stretch"):
            st.session_state.scenarios = initial_scenarios()
            st.session_state.audit = []; st.session_state.chat = []; st.session_state.pending = None
            st.session_state.chat_undo = None
            st.session_state.approval = {"status": "AWAITING APPROVAL", "history": []}
            if data:
                st.session_state.parameters = workbook_parameters(data.settings)
                for name, value in st.session_state.parameters.items():
                    st.session_state[f"param_{name}"] = value
            persist_web_state()
            st.rerun()
st.title("A better day, beautifully managed.")
st.caption("Your hotel at a glance. Your next decision, made clearer.")
with st.container(horizontal=True):
    st.badge("Demo workspace", icon=":material/science:", color="gray")
    if data:
        st.badge(str(data.settings.get("hotel_name", "Hotel")), icon=":material/apartment:", color="green")

if load_error or (validation and not validation.is_valid):
    st.error("🔴 DATA NEEDS ATTENTION\n\nI cannot build today’s operating plan yet.")
    st.write(f"Workbook: `{DATA_PATH}`")
    if load_error: st.caption(load_error)
    if validation:
        for issue in validation.errors: st.write(f"• {issue}")
    st.info("Add or fix the workbook, then select Sync Hotel Data.")
    st.stop()

nav = st.segmented_control("Navigation", ["TODAY", "PARAMETERS", "AI Chat - My Copilot", "OPERATIONS"],
    format_func=lambda label: {"TODAY": ":material/dashboard: Today", "PARAMETERS": ":material/tune: Parameters", "OPERATIONS": ":material/room_service: Operations"}.get(label, label),
    default="TODAY", label_visibility="collapsed", key="navigation", width="stretch")

if "parameters" not in st.session_state:
    st.session_state.parameters = workbook_parameters(data.settings)
for name, value in st.session_state.parameters.items():
    st.session_state.setdefault(f"param_{name}", value)

if nav == "PARAMETERS":
    st.subheader("Shape your operating plan")
    st.caption("Change a driver and the calculated results below update immediately. These values apply to Today, Operations, and AI Chat for this session.")
    hk_tab, inventory_tab = st.tabs(["Housekeeping & staffing", "Inventory & purchasing"])
    with hk_tab:
        a, b = st.columns(2)
        with a.container(border=True):
            st.markdown("#### Room service")
            st.number_input("Checkout room cleaning (minutes)", min_value=1, max_value=240, step=1, key="param_checkout_minutes", help="Cleaning time used for each checkout room.")
            st.number_input("Stayover room cleaning (minutes)", min_value=1, max_value=180, step=1, key="param_stayover_minutes", help="Cleaning time used for each stayover room.")
            st.number_input("Final inspection buffer (minutes)", min_value=0, max_value=120, step=1, key="param_inspection_minutes", help="Added after the final checkout room is cleaned.")
            st.number_input("Late checkout release delay (minutes)", min_value=0, max_value=240, step=5, key="param_late_release_minutes", help="Adds a delay to checkout-room release times.")
        with b.container(border=True):
            st.markdown("#### Team capacity")
            st.number_input("Paid shift length (hours)", min_value=1.0, max_value=16.0, step=0.5, key="param_paid_shift_hours")
            st.number_input("Break time per attendant (minutes)", min_value=0, max_value=360, step=5, key="param_break_minutes")
            st.number_input("Other duties per attendant (minutes)", min_value=0, max_value=360, step=5, key="param_other_duties_minutes")
            st.number_input("Attendant adjustment (add or remove)", min_value=-25, max_value=25, step=1, key="param_attendant_adjustment", help="A negative value models call-outs; a positive value models added coverage.")
    with inventory_tab:
        a, b = st.columns(2)
        with a.container(border=True):
            st.markdown("#### Demand forecast")
            st.number_input("Prior room nights", min_value=1, max_value=100000, step=100, key="param_prior_room_nights", help="Historical activity used to scale product demand.")
            st.number_input("Forecast room nights", min_value=0, max_value=100000, step=100, key="param_forecast_room_nights")
            st.number_input("Forecast horizon (days)", min_value=1, max_value=90, step=1, key="param_forecast_days")
        with b.container(border=True):
            st.markdown("#### Purchasing limits")
            st.number_input("Safety stock (days)", min_value=0, max_value=30, step=1, key="param_safety_days")
            st.number_input("Purchasing budget ($)", min_value=0.0, max_value=1000000.0, step=250.0, key="param_budget_usd")
    updated_parameters = {name: st.session_state[f"param_{name}"] for name in PARAMETER_DEFAULTS}
    if updated_parameters["paid_shift_hours"] * 60 <= updated_parameters["break_minutes"] + updated_parameters["other_duties_minutes"]:
        st.error("Breaks and other duties must leave productive time in the shift. Results below keep the last valid plan.")
    else:
        apply_parameter_values(updated_parameters)

hk, li, timing = calculate_components(data, st.session_state.scenarios, st.session_state.parameters)

if nav == "TODAY":
    st.subheader("Today's overview")
    st.caption(f"Business date · {data.settings.get('business_date', 'Demo day')} · Based on current session assumptions")
    ready = hk["last_checkout_ready"].strftime("%-I:%M %p") if hk["last_checkout_ready"] else "Unavailable"
    a, b = st.columns(2)
    metric_card(a, "Room services", f"{hk['total_rooms']:,}", f"{hk['checkout_rooms']} checkout · {hk['stayover_rooms']} stayover", ":material/bed:")
    metric_card(b, "Checkout readiness", ready, "Projected finish, including final inspection", ":material/schedule:")
    a, b = st.columns(2)
    metric_card(a, "Team coverage", f"{hk['available_attendants']} / {hk['required_attendants']}", "Attendants available / required", ":material/groups:")
    metric_card(b, "Recommended purchase", f"${li['estimated_cost']:,.0f}", f"{li['recommended_bottles']:,.0f} bottles · ${li['budget']:,.0f} budget", ":material/shopping_bag:")
    with st.container(border=True):
        st.markdown("#### Your daily briefing")
        if hk["shortage_minutes"]:
            st.warning(f"Housekeeping needs {hk['shortage_minutes']:,.0f} more productive minutes of coverage.", icon=":material/groups:")
        else:
            st.markdown(f":material/check_circle: **Housekeeping is covered.** {hk['workload_minutes']/60:.1f} hours of cleaning planned.")
        if hk["exceptions"]:
            st.caption(f"{hk['exceptions']} room tasks are projected past their individual deadlines. Review the room plan.")
        if li["remaining_budget"] < 0:
            st.warning(f"The recommended purchase is ${-li['remaining_budget']:,.0f} over budget.", icon=":material/payments:")
        else:
            st.markdown(f":material/account_balance_wallet: **${li['remaining_budget']:,.0f} remains** in the purchasing budget.")
        risk_count = timing.loc[timing.unmet > 0, "sku_id"].nunique()
        if risk_count:
            st.warning(f"{risk_count} inventory items have delivery timing gaps. Review inventory before ordering.", icon=":material/local_shipping:")
        st.button("Discuss today's priorities", type="primary", icon=":material/auto_awesome:", on_click=queue_question, args=("Give me a concise briefing on today's priorities and risks.",))
    with st.container(border=True):
        st.markdown("#### Plan with confidence")
        st.caption("Explore a change, see its impact, and keep every screen in sync.")
        with st.container(horizontal=True):
            st.button("Adjust parameters", icon=":material/tune:", on_click=go_to, args=("PARAMETERS",))
            st.button("Explore operations", icon=":material/arrow_forward:", on_click=go_to, args=("OPERATIONS",))
elif nav == "PARAMETERS":
    st.markdown("### Live calculation preview")
    st.caption("The figures below use your last valid parameter values and active scenarios.")
    h1, h2, h3, h4 = st.columns(4)
    h1.metric("Room services", f"{hk['total_rooms']:,}", f"{hk['checkout_rooms']} checkout · {hk['stayover_rooms']} stayover")
    h2.metric("Cleaning workload", f"{hk['workload_minutes']:,.0f} min", f"{hk['workload_minutes'] / 60:.1f} hours total")
    h3.metric("Attendants required", f"{hk['required_attendants']}", f"{hk['available_attendants']} available")
    h4.metric("Checkout ready", hk["last_checkout_ready"].strftime("%-I:%M %p") if hk["last_checkout_ready"] else "Pending", "includes inspections")
    st.write(f"Workload formula: **{hk['checkout_rooms']} checkout × {st.session_state.scenarios.get('checkout_minutes') or st.session_state.parameters['checkout_minutes']} min + {hk['stayover_rooms']} stayover × {st.session_state.parameters['stayover_minutes']} min**.")
    if hk["shortage_minutes"]:
        st.warning(f"Current staffing leaves {hk['shortage_minutes']:,.0f} minutes of uncovered workload.")
    else:
        st.success("Current staffing covers the calculated room workload.")
    st.space("small")
    i1, i2, i3 = st.columns(3)
    i1.metric("Forecast demand", f"{li['forecast_demand']:,.1f} bottles")
    i2.metric("Recommended order", f"{li['recommended_bottles']:,.0f} bottles", f"estimated ${li['estimated_cost']:,.0f}")
    i3.metric("Budget remaining", f"${li['remaining_budget']:,.0f}", "over budget" if li["remaining_budget"] < 0 else "within budget")
    st.dataframe(li["items"][["product_name", "forecast_demand", "usable_stock", "incoming", "recommended_order", "estimated_cost"]], width="stretch", hide_index=True)
    st.button("Reset Parameters to Workbook Defaults", on_click=reset_parameter_values)
elif nav == "AI Chat - My Copilot":
    st.subheader("AI Chat - My Copilot")
    st.caption("A clearer answer. A better plan. Grounded in your hotel's current data.")
    with st.container(horizontal=True):
        st.badge("Session calculations", icon=":material/calculate:", color="green")
        st.badge("OpenAI configured" if os.getenv("OPENAI_API_KEY", "").strip() else "Local answers", icon=":material/auto_awesome:", color="gray")
    with st.expander("Working context", icon=":material/database:"):
        effective = build_snapshot(st.session_state.scenarios, path=DATA_PATH, parameters=st.session_state.parameters)
        ep = effective["parameters"]
        st.markdown(f"**{hk['checkout_rooms']} checkouts × {ep['checkout_minutes']:g} min** · **{hk['stayover_rooms']} stayovers × {ep['stayover_minutes']:g} min**")
        st.caption(f"{hk['available_attendants']} attendants available · {hk['workload_minutes']:,.0f} cleaning minutes · ${li['budget']:,.0f} purchase budget")
        st.caption("Edits are saved in this browser session. The demo workbook stays unchanged. Readiness is a model projection, not a live room status.")
    queued = st.session_state.pop("queued_question", None)
    if not st.session_state.chat and not queued:
        with st.container(border=True):
            st.markdown("### What would you like to explore?")
            st.write("Understand today's priorities, compare a scenario, or update your plan in a sentence.")
            a, b = st.columns(2)
            if a.button("Brief me on today", icon=":material/wb_sunny:", width="stretch"):
                queued = "Give me a concise briefing on today's priorities and risks."
            if b.button("Explain the staffing plan", icon=":material/groups:", width="stretch"):
                queued = "Explain the staffing calculation and whether we have enough attendants."
            if a.button("Preview +5 checkout minutes", icon=":material/query_stats:", width="stretch"):
                queued = "What if I increase checkout cleaning by 5 minutes?"
            if b.button("Review purchasing risks", icon=":material/shopping_bag:", width="stretch"):
                queued = "Review purchasing budget and inventory delivery risks."
        st.caption("Try: ‘Add 5 minutes to checkout cleaning’ · ‘Set the budget to $9,000’")
    for who, message in st.session_state.chat:
        with st.chat_message(who, avatar=":material/auto_awesome:" if who == "assistant" else ":material/person:"):
            st.markdown(message)
    if st.session_state.chat:
        with st.container(horizontal=True):
            if st.session_state.get("chat_undo") and st.button("Undo last chat change", icon=":material/undo:"):
                undo_chat_change()
                st.rerun()
            if st.button("New conversation", icon=":material/add_comment:"):
                st.session_state.chat = []
                st.rerun()
            st.download_button("Save conversation", "\n\n".join(f"{'You' if who == 'user' else 'My Copilot'}\n{message}" for who, message in st.session_state.chat), file_name="hotel-copilot-conversation.md", mime="text/markdown", icon=":material/download:")
    typed = st.chat_input("Ask a question or change a parameter…", submit_mode="disable")
    prompt = typed or queued
    if prompt:
        st.session_state.chat.append(("user", prompt))
        with st.chat_message("user", avatar=":material/person:"):
            st.markdown(prompt)
        with st.chat_message("assistant", avatar=":material/auto_awesome:"):
            with st.spinner("Checking your current plan…"):
                answer = handle_chat_change(prompt)
                if answer is None:
                    snapshot = build_snapshot(st.session_state.scenarios, path=DATA_PATH, parameters=st.session_state.parameters)
                    history = [{"role": who, "content": body} for who, body in st.session_state.chat[:-1]]
                    answer = answer_question(prompt, snapshot, history)
        st.session_state.chat.append(("assistant", answer))
        st.rerun()
elif nav == "OPERATIONS":
    st.subheader("Operations")
    tab1, tab2 = st.tabs(["Housekeeping", "Bar & inventory"])
    with tab1:
        st.markdown("### Housekeeping coverage")
        st.badge("Workload covered" if hk["shortage_minutes"] == 0 else "Additional coverage needed", color="green" if hk["shortage_minutes"] == 0 else "orange")
        st.write(housekeeping_response(hk).replace("🟢", "").replace("🔴", "").strip())
        c1,c2,c3=st.columns(3)
        c1.metric("Rooms today",hk["total_rooms"]); c2.metric("Checkout rooms",hk["checkout_rooms"]); c3.metric("Stayovers",hk["stayover_rooms"])
        c1,c2,c3=st.columns(3); c1.metric("Staff available",hk["available_attendants"]); c2.metric("Staff required",hk["required_attendants"]); c3.metric("Workload",f"{hk['workload_minutes']/60:.1f} hr")
        if hk["shortage_minutes"]: st.error(f"Required: {hk['workload_minutes']:,.0f} minutes · Available: {hk['available_minutes']:,.0f} · Shortage: {hk['shortage_minutes']:,.0f} minutes. Additional coverage is required.")
        with st.expander("Show me why"):
            st.write(f"{hk['total_rooms']} room services: {hk['checkout_rooms']} checkout and {hk['stayover_rooms']} stayover.")
            st.write(f"{hk['workload_minutes']:,.0f} cleaning minutes ÷ {hk['productive_minutes_per_attendant']:.0f} productive minutes per attendant = {hk['required_attendants']} minimum attendants.")
        st.markdown("#### Quick scenarios")
        a,b,c,d=st.columns(4)
        for col,label,change in [(a,"−1 Attendant",("attendant_adjustment",-1)),(b,"−2 Attendants",("attendant_adjustment",-2)),(c,"Longer Cleaning",("checkout_minutes",float(st.session_state.parameters['checkout_minutes'])+4)),(d,"Late Release",("late_release_minutes",30))]:
            if col.button(label):
                old=st.session_state.scenarios[change[0]]; st.session_state.scenarios[change[0]]= (old+change[1] if change[0]=="attendant_adjustment" else change[1])
                audit_field = "available_attendants" if change[0]=="attendant_adjustment" else change[0]
                audit_old = hk["available_attendants"] if change[0]=="attendant_adjustment" else old
                audit_new = max(0,hk["available_attendants"]+change[1]) if change[0]=="attendant_adjustment" else st.session_state.scenarios[change[0]]
                record_web_change(audit_field,audit_old,audit_new); invalidate(st.session_state.approval,"Operating scenario changed"); persist_web_state(); st.rerun()
        if st.button("Reset Scenario"):
            st.session_state.scenarios=initial_scenarios(); invalidate(st.session_state.approval,"Scenario reset")
            record_web_change("scenario","active","reset", "Manager reset the scenario")
            persist_web_state(); st.rerun()
        if st.toggle("View room plan"): st.dataframe(hk["tasks"], width="stretch", hide_index=True)
    with tab2:
        st.markdown("### Purchasing outlook")
        a,b,c=st.columns(3); a.metric("Forecast demand",f"{li['forecast_demand']:,.1f}"); b.metric("Recommended bottles",f"{li['recommended_bottles']:,.0f}"); c.metric("Estimated purchase",f"${li['estimated_cost']:,.0f}")
        a,b=st.columns(2); a.metric("Budget",f"${li['budget']:,.0f}"); b.metric("Remaining",f"${li['remaining_budget']:,.0f}")
        if li["remaining_budget"]<0: st.error(f"Budget review · ${-li['remaining_budget']:,.0f} over budget. Quantities have not been reduced.")
        risks=timing[timing.unmet>0]
        if not risks.empty: st.error(f"Delivery timing · {risks.unmet.sum():.1f} bottle-equivalent unmet demand in the forecast window.")
        for _,row in li["items"].iterrows():
            with st.expander(f"{row.product_name} · {row.recommended_order:,.0f} bottles · ${row.estimated_cost:,.0f}"):
                st.write(f"Forecast {row.forecast_demand:.1f} · Usable stock {row.usable_stock:.1f} · Confirmed incoming {row.incoming:.1f} · Case pack {int(data.liquor.loc[data.liquor.sku_id==row.sku_id,'case_pack'].iloc[0])}.")
                if "tequila" in str(row.product_name).lower() and st.button("Model delivery on Day 10",key=f"teq-{row.sku_id}"):
                    old=int(st.session_state.scenarios["receipt_day_overrides"].get(row.sku_id, data.purchase_orders.loc[data.purchase_orders.sku_id==row.sku_id,"expected_receipt_day"].iloc[0])); st.session_state.scenarios["receipt_day_overrides"][row.sku_id]=10; record_web_change("expected_receipt_day",old,10); invalidate(st.session_state.approval,"Delivery scenario changed"); persist_web_state(); st.rerun()
        with st.expander("Manager Approval"):
            st.write(st.session_state.approval["status"])
            manager=st.text_input("Manager name"); comments=st.text_area("Comments")
            if st.button("Approve Draft"):
                try:
                    old_status=st.session_state.approval.get("status","AWAITING APPROVAL")
                    approve(st.session_state.approval,manager,comments,"demo-1")
                    record_web_change("approval_status",old_status,"APPROVED DRAFT", "Manager approved draft; no order or schedule sent")
                    persist_web_state(); st.success("Draft approved for review. No order was placed and no schedule was sent.")
                except ValueError as exc: st.warning(str(exc))
            if st.session_state.approval.get("manager"): st.caption(f"Approved by {st.session_state.approval['manager']} · {st.session_state.approval['timestamp']} · version {st.session_state.approval['version']}")
        with st.expander("Audit history"):
            audit_events=state_store.get_audit(WEB_MANAGER_ID)
            if audit_events: st.dataframe(audit_events,width="stretch",hide_index=True)
            else: st.caption("No scenario changes in this session.")

st.caption("HOTEL OPERATIONS · Illustrative demo with synthetic data")

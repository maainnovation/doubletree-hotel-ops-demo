"""Housekeeping capacity model and simple release-aware work plan."""
from datetime import datetime, timedelta
import math
import pandas as pd


AVAILABLE_STATUSES = ["accessible", "available", "yes", "ready", "confirmed"]
HELD_STATUSES = ["yes", "true", "hold"]


def eligible_rooms(rooms):
    """Return rooms that can be included in today's housekeeping plan."""
    return rooms[
        rooms.access_status.astype(str).str.lower().isin(AVAILABLE_STATUSES)
        & ~rooms.maintenance_hold.astype(str).str.lower().isin(HELD_STATUSES)
    ].copy()


def room_count_defaults(rooms):
    """Use eligible workbook room counts as the initial editable plan."""
    eligible = eligible_rooms(rooms)
    return {
        "checkout_rooms": int(eligible.service_type.eq("Checkout").sum()),
        "stayover_rooms": int(eligible.service_type.eq("Stayover").sum()),
    }


def _apply_room_counts(rooms, settings):
    """Resize each service group to its scenario count, using stable room templates."""
    rooms = rooms.copy()
    for service_type, setting in (("Checkout", "checkout_rooms"), ("Stayover", "stayover_rooms")):
        group = rooms[rooms.service_type.eq(service_type)].sort_values(["deadline", "release_time"]).copy()
        if setting not in settings:
            continue
        target = max(0, int(float(settings[setting])))
        if target <= len(group):
            selected = group.head(target).copy()
        elif len(group):
            additions = [group.iloc[[i % len(group)]].copy() for i in range(target - len(group))]
            for i, addition in enumerate(additions, start=len(group) + 1):
                addition.loc[:, "room_id"] = f"PLAN-{service_type.upper()}-{i:04d}"
            selected = pd.concat([group] + additions, ignore_index=True)
        else:
            # A type absent from the workbook can borrow scheduling metadata from
            # another eligible room; if none exist, use a minimal schedule row.
            template = rooms.iloc[0].copy() if not rooms.empty else pd.Series({
                "room_id": "", "service_type": service_type, "release_time": "09:00",
                "deadline": "17:00", "access_status": "Available", "maintenance_hold": "No",
            })
            template["service_type"] = service_type
            selected = pd.DataFrame([template.copy() for _ in range(target)])
        if target and (len(group) == 0 or target <= len(group)):
            selected["service_type"] = service_type
        if target and len(group) == 0:
            selected["room_id"] = [f"PLAN-{service_type.upper()}-{i + 1:04d}" for i in range(target)]
        rooms = rooms[~rooms.service_type.eq(service_type)]
        rooms = pd.concat([rooms, selected], ignore_index=True)
    return rooms


def calculate_housekeeping(data, attendant_adjustment=0, checkout_minutes=None, late_release_minutes=0):
    s = data.settings
    checkout = float(checkout_minutes if checkout_minutes is not None else s["checkout_minutes"])
    stayover = float(s["stayover_minutes"])
    rooms = eligible_rooms(data.rooms)
    rooms = _apply_room_counts(rooms, s)
    rooms["duration"] = rooms.service_type.map({"Checkout": checkout, "Stayover": stayover})
    rooms["release"] = rooms.release_time.map(lambda x: pd.to_datetime(str(x)).to_pydatetime())
    if late_release_minutes:
        checkout_rows = rooms.service_type.eq("Checkout")
        rooms.loc[checkout_rows, "release"] += timedelta(minutes=late_release_minutes)
    total_minutes = float(rooms.duration.sum())
    attendants = data.staff[(data.staff.role == "Room Attendant") & data.staff.available_today.astype(str).str.lower().isin(["yes", "true", "available", "1"]) ]
    available = max(0, len(attendants) + int(attendant_adjustment))
    shift = float(s["paid_shift_hours"]) * 60 - float(s["break_minutes"]) - float(s["other_duties_minutes"])
    required = math.ceil(total_minutes / shift) if shift > 0 else 0
    capacity = available * shift
    # Capacity-level projection; task assignments are calculated by sequentially allocating tasks to the earliest free attendant.
    workers = []
    for _, worker in attendants.iterrows():
        start = datetime.combine(datetime.today().date(), pd.to_datetime(str(worker.shift_start)).time())
        workers.append({"id": worker.staff_id, "free": start})
    if attendant_adjustment < 0:
        workers = workers[:max(0, available)]
    tasks = []
    for _, room in rooms.sort_values(["service_type", "deadline", "release"]).iterrows():
        if not workers: break
        worker = min(workers, key=lambda w: w["free"])
        start = max(worker["free"], room.release)
        finish = start + timedelta(minutes=float(room.duration))
        deadline = datetime.combine(start.date(), pd.to_datetime(str(room.deadline)).time())
        tasks.append({"room_id": room.room_id, "service_type": room.service_type, "staff_id": worker["id"], "start": start, "finish": finish, "deadline": deadline, "late": finish > deadline})
        worker["free"] = finish
    task_frame = pd.DataFrame(tasks)
    checkout_tasks = task_frame[task_frame.service_type == "Checkout"] if not task_frame.empty else task_frame
    last_checkout_clean = checkout_tasks.finish.max() if not checkout_tasks.empty else None
    inspection = int(s.get("inspection_minutes", 0) or 0)
    last_checkout_ready = last_checkout_clean + timedelta(minutes=inspection) if last_checkout_clean else None
    return {"rooms": rooms, "tasks": task_frame, "total_rooms": len(rooms), "checkout_rooms": int((rooms.service_type == "Checkout").sum()), "stayover_rooms": int((rooms.service_type == "Stayover").sum()), "workload_minutes": total_minutes, "productive_minutes_per_attendant": shift, "required_attendants": required, "available_attendants": available, "available_minutes": capacity, "shortage_minutes": max(0, total_minutes-capacity), "last_checkout_clean": last_checkout_clean, "last_checkout_ready": last_checkout_ready, "last_clean": task_frame.finish.max() if not task_frame.empty else None, "exceptions": int(task_frame.late.sum()) if not task_frame.empty else 0}

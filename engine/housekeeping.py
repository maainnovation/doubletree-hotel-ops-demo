"""Housekeeping capacity model and simple release-aware work plan."""
from datetime import datetime, timedelta
import math
import pandas as pd


def calculate_housekeeping(data, attendant_adjustment=0, checkout_minutes=None, late_release_minutes=0):
    s = data.settings
    checkout = float(checkout_minutes if checkout_minutes is not None else s["checkout_minutes"])
    stayover = float(s["stayover_minutes"])
    rooms = data.rooms.copy()
    rooms = rooms[rooms.access_status.astype(str).str.lower().isin(["accessible", "available", "yes", "ready", "confirmed"]) & ~rooms.maintenance_hold.astype(str).str.lower().isin(["yes", "true", "hold"])]
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

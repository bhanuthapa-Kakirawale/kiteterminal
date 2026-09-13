from fastapi import APIRouter

from backend import ui_prefs

router = APIRouter()


@router.get("/api/ui-prefs")
def get_ui_prefs():
    return {"data": ui_prefs.get_prefs()}


@router.post("/api/ui-prefs")
def save_ui_prefs(body: dict):
    return {"data": ui_prefs.save_prefs(**body)}

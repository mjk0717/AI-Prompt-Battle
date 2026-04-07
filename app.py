import base64
import json
import logging
import os
import random
import secrets
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib import request as urllib_request

from flask import Flask, Response, abort, jsonify, render_template, request
from flask_socketio import SocketIO, emit
from google import genai
from google.genai import types


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "log"
IMAGE_DIR = BASE_DIR / "static" / "image"
PROPERTIES_FILE = BASE_DIR / "properties.json"

INITIAL_TEAM_IDS = ["A", "B"]
MAX_ROUNDS = 3
ROUND_SECONDS = 600
MAX_GENERATIONS = 3
MAX_SHARED_PROMPT_LENGTH = 50


def ensure_directories():
    LOG_DIR.mkdir(exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_log_file_path():
    created_at = datetime.now().strftime("%Y%m%d-%H%M%S")
    return LOG_DIR / f"game-{created_at}.log"


def setup_game_logger():
    logger = logging.getLogger("game")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        build_log_file_path(),
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def summarize_text(text: str | None, max_length: int = 200):
    value = (text or "").strip().replace("\r", " ").replace("\n", " ")
    if len(value) <= max_length:
        return value
    return value[: max_length - 3] + "..."


ensure_directories()
game_logger = setup_game_logger()


def log_game_event(event: str, **fields):
    payload = {
        "timestamp": now_iso(),
        "event": event,
        **fields,
    }
    game_logger.info(json.dumps(payload, ensure_ascii=False))


def write_json(path: Path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path, default):
    if not path.exists():
        write_json(path, default)
        return deepcopy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        write_json(path, default)
        return deepcopy(default)


def default_state():
    return {
        "lobby": {
            "manager_session": None,
            "manager_sid": None,
            "clients": {},
            "team_assignments": {team_id: [] for team_id in INITIAL_TEAM_IDS},
            "started": False,
            "manager_settings": {
                "round_durations": [ROUND_SECONDS for _ in range(MAX_ROUNDS)],
                "join_url": "",
            },
        },
        "game": {
            "status": "lobby",
            "active_team_ids": [],
            "current_round": 0,
            "timer_started_at": None,
            "timer_deadline": None,
            "round_durations": [ROUND_SECONDS for _ in range(MAX_ROUNDS)],
            "manager_reference": None,
            "round_references": [],
            "rounds": [],
            "review": None,
            "last_round_result": None,
            "scoreboard": {},
            "final_ranking": [],
        },
    }


def team_label_from_index(index: int):
    if index < 0:
        raise ValueError("Team index must be non-negative.")

    label = ""
    current = index
    while True:
        current, remainder = divmod(current, 26)
        label = chr(ord("A") + remainder) + label
        if current == 0:
            break
        current -= 1
    return label


class JsonStore:
    def __init__(self):
        ensure_directories()
        self.lock = threading.RLock()
        self.pending_generations = set()
        self.pending_review_rounds = set()
        self.bot_controls = {}
        self.bot_counter = 0
        self.generated_media = {}
        self.state = default_state()

    def save_state(self):
        return None

    def get_client(self, session_token: str | None):
        if not session_token:
            return None
        return self.state["lobby"]["clients"].get(session_token)

    def validate_nickname(self, nickname: str):
        normalized = str(nickname or "").strip()
        if not normalized:
            raise ValueError("닉네임을 입력해주세요.")
        return normalized

    def find_client_by_nickname(self, nickname: str):
        for session_token, client in self.state["lobby"]["clients"].items():
            if client["nickname"] == nickname:
                return session_token, client
        return None, None

    def inspect_nickname_entry(self, nickname: str):
        normalized_nickname = self.validate_nickname(nickname)
        session_token, client = self.find_client_by_nickname(normalized_nickname)
        if client:
            return {
                "status": "existing_session",
                "nickname": normalized_nickname,
                "session_token": session_token,
                "client": deepcopy(client),
            }

        return {
            "status": "available_nickname",
            "nickname": normalized_nickname,
        }

    def register_client(self, nickname: str):
        with self.lock:
            normalized_nickname = self.validate_nickname(nickname)

            for session_token, client in self.state["lobby"]["clients"].items():
                if client["nickname"] == normalized_nickname:
                    raise ValueError("이미 진행 중인 닉네임입니다. 기존 세션으로 참여해주세요.")

            session_token = str(uuid.uuid4())
            default_team_id = self.lobby_team_ids()[0]
            client = {
                "session_token": session_token,
                "nickname": normalized_nickname,
                "name": normalized_nickname,
                "connected": False,
                "sid": None,
                "team_id": default_team_id,
                "joined_at": now_iso(),
                "last_seen": now_iso(),
            }
            self.state["lobby"]["clients"][session_token] = client
            self.state["lobby"]["team_assignments"][default_team_id].append(normalized_nickname)
            self.save_state()
            log_game_event(
                "client_registered",
                nickname=normalized_nickname,
                name=normalized_nickname,
                session_token=session_token,
                team_id=default_team_id,
            )
            return session_token, deepcopy(client)

    def restore_client(self, session_token: str | None):
        with self.lock:
            client = self.get_client(session_token)
            if not client:
                return None
            client["last_seen"] = now_iso()
            self.save_state()
            return deepcopy(client)

    def reconnect_client_by_nickname(self, nickname: str):
        with self.lock:
            normalized_nickname = self.validate_nickname(nickname)
            session_token, client = self.find_client_by_nickname(normalized_nickname)
            if not client:
                raise ValueError("해당 닉네임으로 시작된 세션이 없습니다. 먼저 첫 입장을 해주세요.")
            client["last_seen"] = now_iso()
            self.save_state()
            log_game_event(
                "client_reconnected",
                nickname=normalized_nickname,
                name=client["name"],
                session_token=session_token,
                team_id=client.get("team_id"),
            )
            return session_token, deepcopy(client)

    def attach_socket(self, session_token: str, sid: str):
        with self.lock:
            client = self.get_client(session_token)
            if not client:
                return None
            client["connected"] = True
            client["sid"] = sid
            client["last_seen"] = now_iso()
            self.save_state()
            return deepcopy(client)

    def detach_socket(self, sid: str):
        with self.lock:
            for client in self.state["lobby"]["clients"].values():
                if client.get("sid") == sid:
                    client["connected"] = False
                    client["sid"] = None
                    client["last_seen"] = now_iso()
                    self.save_state()
                    return deepcopy(client)
            if self.state["lobby"].get("manager_sid") == sid:
                self.state["lobby"]["manager_sid"] = None
                self.save_state()
            return None

    def attach_manager(self, session_token: str, sid: str):
        with self.lock:
            self.state["lobby"]["manager_session"] = session_token
            self.state["lobby"]["manager_sid"] = sid
            self.save_state()

    def lobby_team_ids(self):
        team_ids = list(self.state["lobby"].get("team_assignments", {}).keys())
        if not team_ids:
            team_ids = list(INITIAL_TEAM_IDS)
            self.state["lobby"]["team_assignments"] = {team_id: [] for team_id in team_ids}
        return team_ids

    def ensure_team_exists(self, team_id: str):
        if team_id not in self.state["lobby"]["team_assignments"]:
            self.state["lobby"]["team_assignments"][team_id] = []

    def create_next_team(self):
        existing = set(self.state["lobby"].get("team_assignments", {}).keys())
        index = 0
        while True:
            team_id = team_label_from_index(index)
            if team_id not in existing:
                self.state["lobby"]["team_assignments"][team_id] = []
                return team_id
            index += 1

    def prune_empty_dynamic_teams(self):
        removed_team_ids = []
        team_assignments = self.state["lobby"].get("team_assignments", {})
        for team_id in list(team_assignments.keys()):
            if team_id in INITIAL_TEAM_IDS:
                continue
            if team_assignments.get(team_id):
                continue
            del team_assignments[team_id]
            removed_team_ids.append(team_id)
        return removed_team_ids

    def assign_team(self, nickname: str, team_id: str | None):
        with self.lock:
            previous_team_id = None
            member_name = None
            if team_id:
                self.ensure_team_exists(team_id)
            for member_ids in self.state["lobby"]["team_assignments"].values():
                if nickname in member_ids:
                    previous_team_id = next(
                        (
                            assigned_team_id
                            for assigned_team_id, assigned_members in self.state["lobby"]["team_assignments"].items()
                            if nickname in assigned_members
                        ),
                        previous_team_id,
                    )
                    member_ids.remove(nickname)
            if team_id:
                self.state["lobby"]["team_assignments"][team_id].append(nickname)
            for client in self.state["lobby"]["clients"].values():
                if client["nickname"] == nickname:
                    client["team_id"] = team_id
                    member_name = client.get("name")
            removed_team_ids = self.prune_empty_dynamic_teams()
            self.save_state()
            log_game_event(
                "team_assigned",
                nickname=nickname,
                name=member_name,
                previous_team_id=previous_team_id,
                team_id=team_id,
                removed_team_ids=removed_team_ids,
            )
            for removed_team_id in removed_team_ids:
                log_game_event("dynamic_team_removed", team_id=removed_team_id, reason="empty")

    def active_team_ids(self):
        active = []
        assigned_nicknames = {
            client["nickname"] for client in self.state["lobby"]["clients"].values() if client.get("team_id")
        }
        for team_id in self.lobby_team_ids():
            members = [
                nickname
                for nickname in self.state["lobby"]["team_assignments"].get(team_id, [])
                if nickname in assigned_nicknames
            ]
            if members:
                active.append(team_id)
        return active

    def reset_game(self):
        with self.lock:
            manager_settings = deepcopy(self.state["lobby"].get("manager_settings") or {})
            for control in self.bot_controls.values():
                control["stop_event"].set()
            self.bot_controls.clear()
            self.generated_media.clear()
            self.state = default_state()
            if manager_settings:
                self.state["lobby"]["manager_settings"] = manager_settings
            self.save_state()
            log_game_event("game_reset")

    def start_game(self, manager_reference: dict | None = None, round_durations: int | list[int] | None = None):
        with self.lock:
            active_team_ids = self.active_team_ids()
            if not active_team_ids:
                raise ValueError("최소 1개 팀에는 인원이 있어야 시작할 수 있습니다.")
            validated_round_durations = self._validate_round_durations(round_durations)
            self.state["lobby"]["started"] = True
            self.state["game"]["status"] = "running"
            self.state["game"]["active_team_ids"] = active_team_ids
            self.state["game"]["current_round"] = 1
            self.state["game"]["round_durations"] = validated_round_durations
            self.state["game"]["round_references"] = build_round_references()
            self.state["game"]["manager_reference"] = None
            self.state["game"]["rounds"] = []
            self.state["game"]["review"] = None
            self.state["game"]["last_round_result"] = None
            self.state["game"]["scoreboard"] = {team_id: 0 for team_id in active_team_ids}
            self.state["game"]["final_ranking"] = []
            self._create_round()
            self.save_state()
            log_game_event(
                "game_started",
                active_team_ids=active_team_ids,
                round_durations=validated_round_durations,
            )

    def _validate_round_durations(self, round_durations: int | list[int] | None):
        if round_durations is None:
            return [ROUND_SECONDS for _ in range(MAX_ROUNDS)]
        if isinstance(round_durations, int):
            round_durations = [round_durations for _ in range(MAX_ROUNDS)]
        if len(round_durations) != MAX_ROUNDS:
            raise ValueError(f"라운드 제한 시간은 {MAX_ROUNDS}개가 필요합니다.")

        validated = []
        for seconds in round_durations:
            try:
                resolved = int(seconds)
            except (TypeError, ValueError):
                raise ValueError("라운드 제한 시간은 숫자여야 합니다.") from None
            if resolved < 10 or resolved > 3600:
                raise ValueError("라운드 제한 시간은 10초 이상 3600초 이하로 입력해주세요.")
            validated.append(resolved)
        return validated

    def update_manager_settings(
        self,
        *,
        round_durations: int | list[int] | None = None,
        join_url: str | None = None,
    ):
        with self.lock:
            settings = self.state["lobby"].setdefault(
                "manager_settings",
                {
                    "round_durations": [ROUND_SECONDS for _ in range(MAX_ROUNDS)],
                    "join_url": "",
                },
            )
            if round_durations is not None:
                settings["round_durations"] = self._validate_round_durations(round_durations)
            if join_url is not None:
                settings["join_url"] = str(join_url).strip()
            self.save_state()
            log_game_event(
                "manager_settings_updated",
                round_durations=deepcopy(settings.get("round_durations", [])),
                join_url=settings.get("join_url", ""),
            )
            return deepcopy(settings)

    def _create_round(self):
        now_ts = int(time.time())
        active_team_ids = self.state["game"]["active_team_ids"] or self.active_team_ids()
        round_number = self.state["game"]["current_round"]
        round_durations = self.state["game"].get("round_durations") or [ROUND_SECONDS for _ in range(MAX_ROUNDS)]
        round_seconds = round_durations[min(max(round_number - 1, 0), len(round_durations) - 1)]
        round_references = self.state["game"].get("round_references") or build_round_references()
        reference = deepcopy(round_references[min(max(round_number - 1, 0), len(round_references) - 1)])
        self.state["game"]["manager_reference"] = reference
        self.state["game"]["timer_started_at"] = now_ts
        self.state["game"]["timer_deadline"] = now_ts + round_seconds
        self.state["game"]["rounds"].append(
            {
                "round_number": round_number,
                "active_team_ids": active_team_ids,
                "status": "running",
                "started_at": now_iso(),
                "duration_seconds": round_seconds,
                "deadline": self.state["game"]["timer_deadline"],
                "reference": reference,
                "teams": {
                    team_id: {
                        "notes": [],
                        "generations_used": 0,
                        "generated_images": [],
                        "selected_image_id": None,
                        "submitted": False,
                        "result_rank": None,
                        "result_score": 0,
                    }
                    for team_id in active_team_ids
                },
                "judge_result": None,
            }
        )
        log_game_event(
            "round_started",
            round_number=round_number,
            active_team_ids=active_team_ids,
            duration_seconds=round_seconds,
            reference_prompt=reference.get("prompt"),
            reference_image_url=reference.get("image_url"),
        )

    def current_round(self):
        rounds = self.state["game"]["rounds"]
        return rounds[-1] if rounds else None

    def add_note(self, team_id: str, text: str, author: str):
        with self.lock:
            round_state = self.current_round()
            if not round_state:
                raise ValueError("진행 중인 라운드가 없습니다.")
            normalized_text = (text or "").strip()
            if not normalized_text:
                raise ValueError("메모를 입력해주세요.")
            if len(normalized_text) > MAX_SHARED_PROMPT_LENGTH:
                raise ValueError(f"팀 공유 프롬프트는 {MAX_SHARED_PROMPT_LENGTH}자까지 입력할 수 있습니다.")
            note = {"id": str(uuid.uuid4()), "text": normalized_text, "author": author, "created_at": now_iso()}
            round_state["teams"][team_id]["notes"].append(note)
            self.save_state()
            log_game_event(
                "note_added",
                round_number=round_state["round_number"],
                team_id=team_id,
                note_id=note["id"],
                author=author,
                text=summarize_text(normalized_text),
            )
            return note["id"]

    def delete_note(self, team_id: str, note_id: str):
        with self.lock:
            round_state = self.current_round()
            if not round_state:
                raise ValueError("진행 중인 라운드가 없습니다.")
            notes = round_state["teams"][team_id]["notes"]
            filtered_notes = [note for note in notes if note["id"] != note_id]
            if len(filtered_notes) == len(notes):
                raise ValueError("삭제할 프롬프트를 찾을 수 없습니다.")
            round_state["teams"][team_id]["notes"] = filtered_notes
            self.save_state()
            log_game_event(
                "note_deleted",
                round_number=round_state["round_number"],
                team_id=team_id,
                note_id=note_id,
            )

    def add_generated_image(self, team_id: str, prompt: str, image_url: str):
        with self.lock:
            round_state = self.current_round()
            team_state = round_state["teams"][team_id]
            team_state["generations_used"] += 1
            image_id = str(uuid.uuid4())
            team_state["generated_images"].append(
                {"id": image_id, "prompt": prompt, "image_url": image_url, "created_at": now_iso()}
            )
            team_state["selected_image_id"] = image_id
            self.save_state()
            return image_id

    def register_generated_media(self, image_bytes: bytes, mime_type: str):
        with self.lock:
            media_id = str(uuid.uuid4())
            self.generated_media[media_id] = {
                "bytes": image_bytes,
                "mime_type": mime_type,
            }
            return f"/generated/{media_id}"

    def get_generated_media(self, media_id: str):
        with self.lock:
            media = self.generated_media.get(media_id)
            if not media:
                return None
            return {
                "bytes": media["bytes"],
                "mime_type": media["mime_type"],
            }

    def start_team_generation(self, round_number: int, team_id: str):
        with self.lock:
            key = (round_number, team_id)
            if key in self.pending_generations:
                return False
            self.pending_generations.add(key)
            return True

    def finish_team_generation(self, round_number: int, team_id: str):
        with self.lock:
            self.pending_generations.discard((round_number, team_id))

    def add_generated_image_if_active(self, round_number: int, team_id: str, prompt: str, image_url: str):
        with self.lock:
            round_state = self.current_round()
            if not round_state or round_state["round_number"] != round_number or round_state["status"] != "running":
                return None

            team_state = round_state["teams"][team_id]
            if team_state["submitted"] or team_state["generations_used"] >= MAX_GENERATIONS:
                return None

            team_state["generations_used"] += 1
            image_id = str(uuid.uuid4())
            team_state["generated_images"].append(
                {"id": image_id, "prompt": prompt, "image_url": image_url, "created_at": now_iso()}
            )
            team_state["selected_image_id"] = image_id
            self.save_state()
            return image_id

    def begin_round_review(self, review_payload: dict):
        with self.lock:
            round_state = self.current_round()
            if not round_state:
                raise ValueError("진행 중인 라운드가 없습니다.")
            round_state["status"] = "review"
            round_state["judge_result"] = review_payload["judge_result"]
            self.state["game"]["status"] = "review"
            self.state["game"]["review"] = review_payload
            self.state["game"]["timer_started_at"] = None
            self.state["game"]["timer_deadline"] = None
            self.save_state()
            log_game_event(
                "round_review_started",
                round_number=review_payload.get("round_number"),
                submitted_team_ids=[item.get("team_id") for item in (review_payload.get("submitted_images") or [])],
                failed_team_ids=[item.get("team_id") for item in (review_payload.get("failed_teams") or [])],
            )

    def update_review_judge_result(self, judge_result: dict, manager_scores: dict | None = None):
        with self.lock:
            if self.state["game"]["status"] != "review" or not self.state["game"]["review"]:
                return
            round_state = self.current_round()
            if not round_state:
                return
            round_state["judge_result"] = judge_result
            self.state["game"]["review"]["judge_result"] = judge_result
            if manager_scores is not None:
                self.state["game"]["review"]["manager_scores"] = manager_scores
            self.save_state()

    def start_review_generation(self, round_number: int):
        with self.lock:
            review_state = self.state["game"].get("review")
            if self.state["game"]["status"] != "review" or not review_state:
                return False
            if int(review_state.get("round_number", 0)) != int(round_number):
                return False
            if round_number in self.pending_review_rounds:
                return False
            self.pending_review_rounds.add(round_number)
            return True

    def finish_review_generation(self, round_number: int):
        with self.lock:
            self.pending_review_rounds.discard(round_number)

    def get_review_payload(self):
        with self.lock:
            review_state = self.state["game"].get("review")
            return deepcopy(review_state) if review_state else None

    def apply_review_scores(self, scores: dict[str, int]):
        with self.lock:
            if self.state["game"]["status"] != "review" or not self.state["game"]["review"]:
                raise ValueError("평가 라운드가 아닙니다.")

            round_state = self.current_round()
            review_state = self.state["game"]["review"]
            round_state["status"] = "finished"

            active_team_ids = [item.get("team_id") for item in (review_state.get("submitted_images") or []) if item.get("team_id")]
            ranking = sorted(
                ({"team_id": team_id, "score": int(scores.get(team_id, 0))} for team_id in active_team_ids),
                key=lambda item: (-item["score"], item["team_id"]),
            )

            for index, item in enumerate(ranking, start=1):
                item["rank"] = index

            review_state["manager_scores"] = {team_id: int(scores.get(team_id, 0)) for team_id in active_team_ids}
            review_state["final_ranking"] = ranking
            round_state["judge_result"] = {
                **(round_state.get("judge_result") or {}),
                "ranking": ranking,
                "manager_scores": review_state["manager_scores"],
            }

            for item in ranking:
                team_id = item["team_id"]
                round_state["teams"][team_id]["result_rank"] = item["rank"]
                round_state["teams"][team_id]["result_score"] = item["score"]
                self.state["game"]["scoreboard"][team_id] += item["score"]

            for failed in (review_state.get("failed_teams") or []):
                team_id = failed.get("team_id")
                if team_id in round_state["teams"]:
                    round_state["teams"][team_id]["result_rank"] = None
                    round_state["teams"][team_id]["result_score"] = 0

            self.state["game"]["last_round_result"] = deepcopy(round_state)
            self.state["game"]["review"] = None

            if self.state["game"]["current_round"] >= MAX_ROUNDS:
                self.state["game"]["status"] = "finished"
                scores = [
                    {"team_id": team_id, "score": score}
                    for team_id, score in self.state["game"]["scoreboard"].items()
                ]
                scores.sort(key=lambda item: item["score"], reverse=True)
                self.state["game"]["final_ranking"] = scores
            else:
                self.state["game"]["status"] = "running"
                self.state["game"]["current_round"] += 1
                self._create_round()

            self.save_state()
            log_game_event(
                "round_scores_applied",
                round_number=round_state["round_number"],
                ranking=ranking,
                scoreboard=deepcopy(self.state["game"]["scoreboard"]),
                game_status=self.state["game"]["status"],
                final_ranking=deepcopy(self.state["game"].get("final_ranking", [])),
            )

    def submit_image(self, team_id: str, image_id: str):
        with self.lock:
            round_state = self.current_round()
            team_state = round_state["teams"][team_id]
            if not any(image["id"] == image_id for image in team_state["generated_images"]):
                raise ValueError("제출할 이미지를 찾을 수 없습니다.")
            team_state["selected_image_id"] = image_id
            team_state["submitted"] = True
            self.save_state()
            log_game_event(
                "image_submitted",
                round_number=round_state["round_number"],
                team_id=team_id,
                image_id=image_id,
            )

    def select_image(self, team_id: str, image_id: str):
        with self.lock:
            round_state = self.current_round()
            if not round_state or round_state["status"] != "running":
                raise ValueError("Image selection is only available during a running round.")

            team_state = round_state["teams"][team_id]
            if team_state["submitted"]:
                raise ValueError("Submitted teams cannot change their selected image.")
            if not any(image["id"] == image_id for image in team_state["generated_images"]):
                raise ValueError("Selected image could not be found.")

            team_state["selected_image_id"] = image_id
            self.save_state()
            log_game_event(
                "image_selected",
                round_number=round_state["round_number"],
                team_id=team_id,
                image_id=image_id,
            )

    def all_teams_submitted(self):
        round_state = self.current_round()
        if not round_state:
            return False
        return all(round_state["teams"][team_id]["submitted"] for team_id in round_state.get("active_team_ids", []))

    def serialize_public_state(self):
        with self.lock:
            clients = []
            for client in self.state["lobby"]["clients"].values():
                clients.append(
                    {
                        "nickname": client["nickname"],
                        "name": client["name"],
                        "is_bot": bool(client.get("is_bot")),
                        "connected": client["connected"],
                        "team_id": client["team_id"],
                    }
                )

            public_game = deepcopy(self.state["game"])
            current_round = public_game["rounds"][-1] if public_game.get("rounds") else None
            if current_round:
                round_number = current_round["round_number"]
                for team_id, team_state in current_round.get("teams", {}).items():
                    team_state["is_generating"] = (round_number, team_id) in self.pending_generations
            if public_game.get("review"):
                review_round_number = public_game["review"].get("round_number")
                public_game["review"]["is_judging"] = review_round_number in self.pending_review_rounds

            return {
                "config": {
                    "max_rounds": MAX_ROUNDS,
                    "max_generations": MAX_GENERATIONS,
                    "max_shared_prompt_length": MAX_SHARED_PROMPT_LENGTH,
                },
                "lobby": {
                    "started": self.state["lobby"]["started"],
                    "clients": sorted(clients, key=lambda item: item["name"]),
                    "team_assignments": deepcopy(self.state["lobby"]["team_assignments"]),
                    "manager_settings": deepcopy(
                        self.state["lobby"].get("manager_settings")
                        or {
                            "round_durations": [ROUND_SECONDS for _ in range(MAX_ROUNDS)],
                            "join_url": "",
                        }
                    ),
                },
                "game": public_game,
            }

    def create_test_bot(self, team_id: str):
        with self.lock:
            self.ensure_team_exists(team_id)
            self.bot_counter += 1
            session_token = f"test-bot-session-{self.bot_counter}"
            nickname = f"test-bot-{self.bot_counter}"
            client = {
                "session_token": session_token,
                "nickname": nickname,
                "name": f"테스트 봇 {self.bot_counter}",
                "connected": True,
                "sid": None,
                "team_id": team_id,
                "is_bot": True,
                "joined_at": now_iso(),
                "last_seen": now_iso(),
            }
            self.state["lobby"]["clients"][session_token] = client
            self.state["lobby"]["team_assignments"][team_id].append(nickname)
            stop_event = threading.Event()
            self.bot_controls[session_token] = {"stop_event": stop_event, "note_id": None}
            self.save_state()
            return deepcopy(client), stop_event


store = JsonStore()
app = Flask(__name__)
app.config["SECRET_KEY"] = secrets.token_urlsafe(48)
socketio = SocketIO(app, cors_allowed_origins="*")


def placeholder_image(prompt: str, seed: str):
    safe_prompt = prompt[:120]
    safe_seed = seed[:24]
    svg = f"""
    <svg xmlns="http://www.w3.org/2000/svg" width="512" height="512">
      <rect width="100%" height="100%" fill="#f2efe8"/>
      <rect x="24" y="24" width="464" height="464" rx="28" fill="#fffaf3" stroke="#1f2937" stroke-width="4"/>
      <circle cx="256" cy="190" r="72" fill="#d9a66b" />
      <circle cx="232" cy="180" r="8" fill="#1f2937" />
      <circle cx="280" cy="180" r="8" fill="#1f2937" />
      <path d="M220 222 Q256 250 292 222" fill="none" stroke="#1f2937" stroke-width="6" stroke-linecap="round"/>
      <text x="256" y="330" text-anchor="middle" font-size="24" font-family="Verdana" fill="#1f2937">AI Preview</text>
      <text x="256" y="366" text-anchor="middle" font-size="16" font-family="Verdana" fill="#374151">{safe_seed}</text>
      <foreignObject x="60" y="390" width="392" height="90">
        <div xmlns="http://www.w3.org/1999/xhtml" style="font-family: Verdana; font-size: 14px; color: #374151; text-align:center; line-height:1.35;">
          {safe_prompt}
        </div>
      </foreignObject>
    </svg>
    """.strip()
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def build_round_references():
    references = []
    supported_extensions = ["png", "jpg", "jpeg", "webp"]
    for round_number in range(1, MAX_ROUNDS + 1):
        candidate_urls = []
        stage_dir = IMAGE_DIR / f"stage{round_number}"
        if stage_dir.exists() and stage_dir.is_dir():
            for extension in supported_extensions:
                candidate_urls.extend(
                    f"/static/image/stage{round_number}/{path.name}"
                    for path in sorted(stage_dir.glob(f"*.{extension}"))
                    if path.is_file()
                )
        for extension in supported_extensions:
            candidate_urls.extend(
                f"/static/image/{path.name}"
                for path in sorted(IMAGE_DIR.glob(f"round{round_number}-*.{extension}"))
                if path.is_file()
            )
        if not candidate_urls:
            for extension in supported_extensions:
                image_path = IMAGE_DIR / f"round{round_number}.{extension}"
                if image_path.exists():
                    candidate_urls.append(f"/static/image/round{round_number}.{extension}")
        if not candidate_urls:
            image_url = placeholder_image(f"Round {round_number} reference image", f"Round {round_number}")
        else:
            image_url = random.choice(candidate_urls)
        references.append(
            {
                "round_number": round_number,
                "prompt": f"Round {round_number} reference image",
                "image_url": image_url,
            }
        )
    return references


def load_properties():
    if not PROPERTIES_FILE.exists():
        return {}
    try:
        return json.loads(PROPERTIES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def detect_image_mime(image_bytes: bytes):
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return "application/octet-stream"


def encode_image_bytes(image_bytes: bytes, mime_type: str | None = None):
    resolved_mime_type = mime_type or detect_image_mime(image_bytes)
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{resolved_mime_type};base64,{encoded}"


def parse_json_from_text(text: str):
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return json.loads(stripped)


def sanitize_response_for_logging(payload):
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return f"<omitted binary length={len(payload)}>"

    if isinstance(payload, dict):
        sanitized = {}
        for key, value in payload.items():
            if key in {"result", "b64_json", "image_base64", "image_bytes"} and isinstance(value, str):
                sanitized[key] = f"<omitted base64 length={len(value)}>"
            elif key in {"data", "bytes", "inline_data", "image_bytes"} and isinstance(value, (bytes, bytearray, memoryview)):
                sanitized[key] = f"<omitted binary length={len(value)}>"
            elif key in {"image_url", "url"} and isinstance(value, str):
                if value.startswith("data:image/"):
                    sanitized[key] = "<omitted data url image>"
                else:
                    sanitized[key] = value
            else:
                sanitized[key] = sanitize_response_for_logging(value)
        return sanitized

    if isinstance(payload, list):
        return [sanitize_response_for_logging(item) for item in payload]

    return payload


def log_responses_api_payload(label: str, payload):
    try:
        sanitized = sanitize_response_for_logging(payload)
        print(
            json.dumps(
                {
                    "event": label,
                    "response": sanitized,
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
    except Exception as exc:
        print(f"[{label}] failed to log response payload: {exc}", flush=True)


def compact_log_payload(payload, *, max_depth=3, max_items=5, max_string_length=240):
    if max_depth <= 0:
        return f"<{type(payload).__name__}>"

    if isinstance(payload, dict):
        compacted = {}
        for index, (key, value) in enumerate(payload.items()):
            if index >= max_items:
                compacted["<truncated_keys>"] = len(payload) - max_items
                break
            compacted[key] = compact_log_payload(
                value,
                max_depth=max_depth - 1,
                max_items=max_items,
                max_string_length=max_string_length,
            )
        return compacted

    if isinstance(payload, list):
        items = [
            compact_log_payload(
                item,
                max_depth=max_depth - 1,
                max_items=max_items,
                max_string_length=max_string_length,
            )
            for item in payload[:max_items]
        ]
        if len(payload) > max_items:
            items.append(f"<truncated_items:{len(payload) - max_items}>")
        return items

    if isinstance(payload, str):
        return payload if len(payload) <= max_string_length else f"{payload[:max_string_length]}...<truncated>"

    return payload


def resolve_gemini_api_key(api_config: dict):
    return str(os.environ.get("GEMINI_API_KEY", "")).strip()


def load_image_bytes_for_gemini(image_url: str):
    if not isinstance(image_url, str) or not image_url:
        raise ValueError("Image URL is required.")

    if image_url.startswith("data:image/"):
        header, encoded = image_url.split(",", 1)
        mime_type = header.split(";")[0].split(":", 1)[1]
        return base64.b64decode(encoded), mime_type

    if image_url.startswith("/static/"):
        local_path = BASE_DIR / image_url.lstrip("/")
        if local_path.exists() and local_path.is_file():
            image_bytes = local_path.read_bytes()
            return image_bytes, detect_image_mime(image_bytes)

    if image_url.startswith("/generated/"):
        media_id = image_url.rsplit("/", 1)[-1]
        media = store.get_generated_media(media_id)
        if media:
            return media["bytes"], media["mime_type"]

    with urllib_request.urlopen(image_url, timeout=int(load_properties().get("image_api", {}).get("timeout", 60))) as response:
        image_bytes = response.read()
        mime_type = response.headers.get_content_type()
    return image_bytes, mime_type


def build_gemini_image_part(image_url: str):
    image_bytes, mime_type = load_image_bytes_for_gemini(image_url)
    return types.Part.from_bytes(data=image_bytes, mime_type=mime_type)


def store_generated_image_reference(image_url: str):
    image_bytes, mime_type = load_image_bytes_for_gemini(image_url)
    return store.register_generated_media(image_bytes, mime_type)


def serialize_gemini_payload(payload):
    if payload is None:
        return None
    if isinstance(payload, list):
        return [serialize_gemini_payload(item) for item in payload]
    if isinstance(payload, dict):
        return {key: serialize_gemini_payload(value) for key, value in payload.items()}
    if hasattr(payload, "model_dump"):
        return payload.model_dump(exclude_none=True)
    if hasattr(payload, "to_json_dict"):
        return payload.to_json_dict()
    return payload


def build_gemini_config(api_config: dict):
    raw_generation_config = deepcopy(api_config.get("generation_config") or {})

    response_modalities = raw_generation_config.get("response_modalities") or raw_generation_config.get("responseModalities")
    image_config = raw_generation_config.get("image_config") or raw_generation_config.get("imageConfig")
    response_mime_type = raw_generation_config.get("response_mime_type") or raw_generation_config.get("responseMimeType")
    temperature = raw_generation_config.get("temperature")

    config_kwargs = {}
    if response_modalities:
        config_kwargs["response_modalities"] = list(response_modalities)
    if image_config:
        aspect_ratio = image_config.get("aspect_ratio") or image_config.get("aspectRatio")
        if aspect_ratio:
            config_kwargs["image_config"] = types.ImageConfig(aspect_ratio=aspect_ratio)
    if response_mime_type:
        config_kwargs["response_mime_type"] = str(response_mime_type)
    if temperature is not None:
        config_kwargs["temperature"] = float(temperature)

    system_instruction = str(api_config.get("system_instruction", "")).strip()
    if system_instruction:
        config_kwargs["system_instruction"] = system_instruction

    return types.GenerateContentConfig(**config_kwargs)


def get_gemini_client(api_config: dict):
    api_key = resolve_gemini_api_key(api_config)
    if not api_key:
        raise RuntimeError("Gemini API configuration is incomplete.")
    return genai.Client(api_key=api_key)


def resolve_gemini_max_input_tokens(api_config: dict):
    configured_limit = api_config.get("max_input_tokens")
    if configured_limit is not None:
        try:
            return int(configured_limit)
        except (TypeError, ValueError):
            pass

    model_name = str(api_config.get("model", "")).strip()
    model_limits = {
        "gemini-2.5-flash-image": 65536,
    }
    return model_limits.get(model_name, 65536)


def count_gemini_input_tokens(api_config: dict, contents):
    client = get_gemini_client(api_config)
    try:
        response = client.models.count_tokens(
            model=str(api_config.get("model", "")).strip(),
            contents=contents,
        )
    except Exception as exc:
        raise RuntimeError(f"Gemini token counting failed: {exc}") from exc

    total_tokens = getattr(response, "total_tokens", None)
    if total_tokens is None and isinstance(response, dict):
        total_tokens = response.get("total_tokens")
    if total_tokens is None:
        serialized = serialize_gemini_payload(response) or {}
        total_tokens = serialized.get("total_tokens")
    if total_tokens is None:
        raise RuntimeError("Gemini token counting did not return total_tokens.")
    return int(total_tokens)


def ensure_gemini_input_within_limit(api_config: dict, contents):
    input_tokens = count_gemini_input_tokens(api_config, contents)
    max_input_tokens = resolve_gemini_max_input_tokens(api_config)

    log_responses_api_payload(
        "gemini_input_token_count",
        {
            "model": str(api_config.get("model", "")).strip(),
            "input_tokens": input_tokens,
            "max_input_tokens": max_input_tokens,
        },
    )

    if input_tokens > max_input_tokens:
        raise ValueError(
            f"이미지 생성 요청이 너무 깁니다. 현재 입력 토큰은 {input_tokens}개이고, "
            f"허용 최대치는 {max_input_tokens}개입니다. 프롬프트를 더 짧게 줄여주세요."
        )

    return input_tokens


def extract_text_from_gemini_payload(payload: dict):
    serialized = serialize_gemini_payload(payload) or {}
    direct_text = serialized.get("text")
    if isinstance(direct_text, str) and direct_text.strip():
        return direct_text.strip()

    parsed = serialized.get("parsed")
    if isinstance(parsed, (dict, list)) and parsed:
        return json.dumps(parsed, ensure_ascii=False)

    candidates = serialized.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("Gemini response did not include candidates.")

    text_parts = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_text = candidate.get("text")
        if isinstance(candidate_text, str) and candidate_text.strip():
            text_parts.append(candidate_text)

        candidate_parsed = candidate.get("parsed")
        if isinstance(candidate_parsed, (dict, list)) and candidate_parsed:
            text_parts.append(json.dumps(candidate_parsed, ensure_ascii=False))

        content = candidate.get("content") or candidate.get("content_dict") or {}
        parts = content.get("parts") or []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str) and part["text"].strip():
                text_parts.append(part["text"])
                continue

            part_inline_text = part.get("inline_text")
            if isinstance(part_inline_text, str) and part_inline_text.strip():
                text_parts.append(part_inline_text)
                continue

            part_parsed = part.get("parsed")
            if isinstance(part_parsed, (dict, list)) and part_parsed:
                text_parts.append(json.dumps(part_parsed, ensure_ascii=False))

    combined = "".join(text_parts).strip()
    if not combined:
        log_responses_api_payload(
            "gemini_text_missing",
            {
                "payload_preview": compact_log_payload(sanitize_response_for_logging(serialized)),
            },
        )
        raise ValueError("Gemini response did not include text.")
    return combined


def extract_image_from_gemini_payload(payload: dict):
    serialized = serialize_gemini_payload(payload) or {}
    candidates = serialized.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("Gemini response did not include candidates.")

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content") or {}
        parts = content.get("parts") or []
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline_data = part.get("inlineData") or part.get("inline_data") or {}
            encoded = inline_data.get("data")
            if isinstance(encoded, str) and encoded:
                mime_type = inline_data.get("mimeType") or inline_data.get("mime_type") or "image/png"
                return f"data:{mime_type};base64,{encoded}"
            if isinstance(encoded, (bytes, bytearray, memoryview)):
                mime_type = inline_data.get("mimeType") or inline_data.get("mime_type") or "image/png"
                encoded_bytes = bytes(encoded)
                if encoded_bytes:
                    encoded_base64 = base64.b64encode(encoded_bytes).decode("ascii")
                    return f"data:{mime_type};base64,{encoded_base64}"

            if encoded is not None:
                log_responses_api_payload(
                    "gemini_image_inline_data_unhandled",
                    {
                        "encoded_type": type(encoded).__name__,
                        "mime_type": inline_data.get("mimeType") or inline_data.get("mime_type") or "image/png",
                    },
                )

    raise ValueError("Gemini response did not include an image.")

def call_gemini_api(api_config: dict, contents):
    client = get_gemini_client(api_config)
    timeout_seconds = int(api_config.get("timeout", 180))
    try:
        response = client.models.generate_content(
            model=str(api_config.get("model", "")).strip(),
            contents=contents,
            config=build_gemini_config(api_config),
        )
    except Exception as exc:
        raise RuntimeError(f"Gemini API request failed: {exc}") from exc
    return serialize_gemini_payload(response)


def merge_accumulated_text_into_gemini_payload(payload: dict, accumulated_text: str):
    if not isinstance(payload, dict) or not accumulated_text.strip():
        return payload

    payload["text"] = accumulated_text

    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return payload

    first_candidate = candidates[0]
    if not isinstance(first_candidate, dict):
        return payload

    content = first_candidate.get("content")
    if not isinstance(content, dict):
        content = {}
        first_candidate["content"] = content

    parts = content.get("parts")
    if not isinstance(parts, list):
        parts = []
        content["parts"] = parts

    if parts and isinstance(parts[0], dict):
        existing_text = parts[0].get("text")
        if not isinstance(existing_text, str) or not existing_text.strip():
            parts[0]["text"] = accumulated_text
    else:
        parts.insert(0, {"text": accumulated_text})

    return payload


def stream_gemini_api(api_config: dict, contents, on_delta=None):
    client = get_gemini_client(api_config)
    stream_id = uuid.uuid4().hex[:8]
    chunk_count = 0
    accumulated_text = ""
    final_payload = None

    log_responses_api_payload(
        "gemini_api_stream_start",
        {
            "stream_id": stream_id,
            "model": str(api_config.get("model", "")).strip(),
            "request_payload": compact_log_payload(sanitize_response_for_logging(serialize_gemini_payload(contents))),
        },
    )

    try:
        for chunk in client.models.generate_content_stream(
            model=str(api_config.get("model", "")).strip(),
            contents=contents,
            config=build_gemini_config(api_config),
        ):
            payload_chunk = serialize_gemini_payload(chunk)
            chunk_count += 1
            final_payload = payload_chunk
            chunk_text = ""
            try:
                chunk_text = extract_text_from_gemini_payload(payload_chunk)
            except ValueError:
                chunk_text = ""
            if chunk_text:
                if chunk_text.startswith(accumulated_text):
                    accumulated_text = chunk_text
                else:
                    accumulated_text += chunk_text
                if on_delta:
                    on_delta(accumulated_text)
            log_responses_api_payload(
                "gemini_api_stream_chunk",
                {
                    "stream_id": stream_id,
                    "chunk_index": chunk_count,
                    "text_length": len(chunk_text),
                    "payload": compact_log_payload(sanitize_response_for_logging(payload_chunk)),
                },
            )
    except Exception as exc:
        raise RuntimeError(f"Gemini API stream failed: {exc}") from exc

    if final_payload is None:
        raise RuntimeError("Gemini API stream completed without a final payload.")

    if accumulated_text.strip():
        final_payload = merge_accumulated_text_into_gemini_payload(final_payload, accumulated_text)

    log_responses_api_payload(
        "gemini_api_stream_done",
        {
            "stream_id": stream_id,
            "chunk_count": chunk_count,
            "accumulated_text_length": len(accumulated_text),
            "final_payload": compact_log_payload(sanitize_response_for_logging(final_payload)),
        },
    )
    return final_payload


def generate_image_with_gemini(prompt: str):
    image_api_config = deepcopy(load_properties().get("image_api", {}))
    if not resolve_gemini_api_key(image_api_config):
        time.sleep(5)
        return placeholder_image(prompt, "Stub Image")

    ensure_gemini_input_within_limit(image_api_config, prompt)
    response_payload = call_gemini_api(image_api_config, prompt)
    log_responses_api_payload("gemini_image_api_response", response_payload)
    return extract_image_from_gemini_payload(response_payload)


def judge_images(reference_prompt: str, reference_image_url: str, images: list[dict]):
    similarity_map = {"B": 91, "A": 84, "C": 77}
    return {
        "assistant_message": "테스트 평가 스텁입니다. 전체적으로 B팀은 구도와 색감이 가장 유사하고, A팀은 핵심 피사체가 비슷하며, C팀은 분위기는 가깝지만 디테일 차이가 큽니다. 최종 점수는 매니저가 직접 입력해주세요.",
        "similarities": [
            {"team_id": image["team_id"], "similarity": similarity_map.get(image["team_id"], 70)}
            for image in images
        ],
    }


def normalize_judge_result(parsed: dict, response_text: str):
    scores = parsed.get("scores") or []
    if not parsed.get("similarities"):
        parsed["similarities"] = [
            {"team_id": item.get("team_id"), "similarity": int(item.get("score", 0))}
            for item in scores
            if item.get("team_id")
        ]
    if not parsed.get("ranking"):
        ranking = sorted(
            (
                {"team_id": item.get("team_id"), "score": int(item.get("score", 0))}
                for item in scores
                if item.get("team_id")
            ),
            key=lambda item: (-item["score"], item["team_id"]),
        )
        for index, item in enumerate(ranking, start=1):
            item["rank"] = index
        parsed["ranking"] = ranking

    parsed["assistant_message"] = parsed.get("assistant_message") or response_text
    return parsed


def extract_assistant_message_preview(text: str):
    if not isinstance(text, str):
        return "AI 평가를 생성 중입니다..."

    stripped = text.strip()
    if not stripped:
        return "AI 평가를 생성 중입니다..."
    if '"assistant_message"' not in stripped:
        return stripped if not stripped.startswith("{") else "AI 평가를 생성 중입니다..."

    key_index = stripped.find('"assistant_message"')
    colon_index = stripped.find(":", key_index)
    if colon_index < 0:
        return "AI 평가를 생성 중입니다..."

    value_start = stripped.find('"', colon_index)
    if value_start < 0:
        return "AI 평가를 생성 중입니다..."

    chars = []
    escaped = False
    for char in stripped[value_start + 1:]:
        if escaped:
            if char == "n":
                chars.append("\n")
            elif char == "t":
                chars.append("\t")
            else:
                chars.append(char)
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            break
        chars.append(char)

    preview = "".join(chars).strip()
    return preview or "AI 평가를 생성 중입니다..."


def build_judge_content(reference_prompt: str, reference_image_url: str, images: list[dict]):
    content = [
        types.Part.from_text(
            text=(
                "The first image is the reference image. Compare each team submission against the reference image. "
                "Return JSON only with this schema: "
                '{"assistant_message":"string","scores":[{"team_id":"A","score":0,"reason":"string"}],'
                '"ranking":[{"team_id":"A","score":0,"rank":1}],"similarities":[{"team_id":"A","similarity":0}]}. '
                "Scores and similarities must be integers from 0 to 100. "
                "Score each team carefully by checking composition, main subject match, pose or arrangement, color and lighting, background elements, detail accuracy, and overall completeness. "
                "In every reason field, explain the score in detail by mentioning multiple specific visual elements that matched well or differed from the reference image. "
                "The assistant_message should also summarize the strongest and weakest visual points for each team in detail. "
                "All natural-language output must be written in Korean only. "
                "Write assistant_message and every reason field in Korean only, with no English words unless they are team_id values inside the JSON schema. "
                f"Reference prompt: {reference_prompt}"
            )
        ),
        build_gemini_image_part(reference_image_url),
    ]

    for image in images:
        content.append(types.Part.from_text(text=f"Team {image['team_id']} submission."))
        content.append(build_gemini_image_part(image["image_url"]))
    return content


def judge_images_with_gemini(reference_prompt: str, reference_image_url: str, images: list[dict], on_delta=None):
    properties = load_properties()
    judge_api_config = deepcopy(properties.get("judge_api", {}))
    if not resolve_gemini_api_key(judge_api_config):
        return judge_images(reference_prompt, reference_image_url, images)

    contents = [
        types.Content(
            role="user",
            parts=build_judge_content(reference_prompt, reference_image_url, images),
        )
    ]

    should_stream = bool(judge_api_config.get("stream", True)) and on_delta is not None
    if should_stream:
        response_payload = stream_gemini_api(judge_api_config, contents, on_delta=on_delta)
    else:
        response_payload = call_gemini_api(judge_api_config, contents)
    log_responses_api_payload("gemini_judge_api_response", response_payload)
    response_text = extract_text_from_gemini_payload(response_payload)
    parsed = parse_json_from_text(response_text)
    return normalize_judge_result(parsed, response_text)


def launch_judge_review(round_number: int, reference: dict, submitted_images: list[dict], active_team_ids: list[str]):
    if not store.start_review_generation(round_number):
        return False
    threading.Thread(
        target=run_judge_review_async,
        args=(round_number, deepcopy(reference), deepcopy(submitted_images), list(active_team_ids)),
        daemon=True,
    ).start()
    return True


def public_state():
    return store.serialize_public_state()


def broadcast_state():
    socketio.emit("state:update", public_state())


def build_client_payload(session_token: str):
    client = store.get_client(session_token)
    return {"sessionToken": session_token, "client": client, "state": public_state()}


def run_test_bot_loop(session_token: str, stop_event: threading.Event):
    bot_prompt = "안녕하세요 테스트 봇 입니다."

    while not stop_event.wait(3):
        client = store.get_client(session_token)
        if not client:
            return

        team_id = client.get("team_id")
        if not team_id or store.state["game"].get("status") != "running":
            continue

        round_state = store.current_round()
        if not round_state or round_state.get("status") != "running":
            continue

        control = store.bot_controls.get(session_token)
        if not control:
            return

        note_id = control.get("note_id")
        try:
            if note_id:
                store.delete_note(team_id, note_id)
                control["note_id"] = None
            else:
                control["note_id"] = store.add_note(team_id, bot_prompt, client["name"])
            broadcast_state()
        except ValueError:
            control["note_id"] = None


def run_judge_review_async(round_number: int, reference: dict, submitted_images: list[dict], active_team_ids: list[str]):
    assistant_message = ""
    manager_scores = {
        team_id: int((store.get_review_payload() or {}).get("manager_scores", {}).get(team_id, 0))
        for team_id in active_team_ids
    }

    def on_delta(text: str):
        nonlocal assistant_message
        assistant_message = extract_assistant_message_preview(text)
        store.update_review_judge_result(
            {
                "assistant_message": assistant_message or "AI 평가를 생성 중입니다...",
                "similarities": [],
                "scores": [],
                "ranking": [],
                "status": "streaming",
            },
            manager_scores=manager_scores,
        )

    try:
        judge_result = judge_images_with_gemini(reference["prompt"], reference["image_url"], submitted_images, on_delta=on_delta)
        judge_result["status"] = "completed"
        store.update_review_judge_result(
            judge_result,
            manager_scores=manager_scores,
        )
    except Exception as exc:
        store.update_review_judge_result(
            {
                "assistant_message": f"AI 평가 생성에 실패했습니다: {exc}",
                "similarities": [],
                "scores": [],
                "ranking": [],
                "status": "error",
            },
            manager_scores=manager_scores,
        )
    finally:
        store.finish_review_generation(round_number)
        broadcast_state()


def run_team_image_generation_async(
    *,
    request_sid: str,
    round_number: int,
    team_id: str,
    prompt: str,
):
    should_broadcast = False
    try:
        generated_image = generate_image_with_gemini(prompt)
        image_url = store_generated_image_reference(generated_image)
        image_id = store.add_generated_image_if_active(round_number, team_id, prompt, image_url)
        if image_id is None:
            log_game_event(
                "image_generation_discarded",
                round_number=round_number,
                team_id=team_id,
                prompt=summarize_text(prompt),
            )
            socketio.emit(
                "session:error",
                {"message": "이미지 생성이 완료되었지만 현재 라운드 상태가 변경되어 결과를 반영하지 못했습니다."},
                room=request_sid,
            )
            return

        log_game_event(
            "image_generated",
            round_number=round_number,
            team_id=team_id,
            image_id=image_id,
            prompt=summarize_text(prompt),
            image_url=image_url,
        )

        socketio.emit(
            "team:image_generated",
            {"teamId": team_id, "imageId": image_id, "imageUrl": image_url},
            room=request_sid,
        )
        should_broadcast = True
    except Exception as exc:
        log_game_event(
            "image_generation_failed",
            round_number=round_number,
            team_id=team_id,
            prompt=summarize_text(prompt),
            error=str(exc),
        )
        socketio.emit(
            "session:error",
            {"message": f"이미지 생성 요청에 실패했습니다: {exc}"},
            room=request_sid,
        )
        should_broadcast = True
    finally:
        store.finish_team_generation(round_number, team_id)
        broadcast_state()
        broadcast_state()
        if should_broadcast:
            broadcast_state()


def maybe_finish_round(force: bool = False):
    round_state = store.current_round()
    if not round_state or round_state["status"] != "running":
        return
    broadcast_state()
    if not force and not store.all_teams_submitted() and int(time.time()) < round_state["deadline"]:
        return

    active_team_ids = round_state.get("active_team_ids", store.state["game"].get("active_team_ids", []))
    submitted_images = []
    failed_teams = []
    for team_id in active_team_ids:
        team_state = round_state["teams"][team_id]
        selected = next(
            (image for image in team_state["generated_images"] if image["id"] == team_state["selected_image_id"]),
            None,
        )
        if selected:
            team_state["submitted"] = True
            submitted_images.append(
                {
                    "team_id": team_id,
                    "image_url": selected["image_url"],
                    "prompt": selected["prompt"],
                    "submitted": True,
                }
            )
        else:
            failed_teams.append({"team_id": team_id, "submitted": False})

    reference = round_state.get("reference") or store.state["game"]["manager_reference"]
    judged_team_ids = [item["team_id"] for item in submitted_images]
    judge_result = {
        "assistant_message": "AI 평가를 생성 중입니다..." if judged_team_ids else "제출한 팀이 없어 AI 평가를 건너뜁니다.",
        "similarities": [],
        "scores": [],
        "ranking": [],
        "status": "streaming" if judged_team_ids else "skipped",
    }
    store.begin_round_review(
        {
            "round_number": round_state["round_number"],
            "reference": deepcopy(reference),
            "submitted_images": submitted_images,
            "failed_teams": failed_teams,
            "judge_result": judge_result,
            "manager_scores": {team_id: 0 for team_id in judged_team_ids},
        }
    )
    broadcast_state()
    if judged_team_ids:
        launch_judge_review(round_state["round_number"], reference, submitted_images, judged_team_ids)


def timer_loop():
    while True:
        time.sleep(1)
        if store.state["game"]["status"] != "running":
            continue
        round_state = store.current_round()
        if round_state and round_state["status"] == "running" and int(time.time()) >= round_state["deadline"]:
            maybe_finish_round()


@app.get("/")
def client_page():
    return render_template("index.html")


@app.get("/manager")
def manager_page():
    return render_template("manager.html")


@app.get("/api/bootstrap")
def bootstrap():
    role = request.args.get("role", "client")
    session_token = request.args.get("sessionToken")
    if role == "manager":
        return jsonify({"state": public_state()})
    return jsonify({"state": public_state(), "session": store.restore_client(session_token)})


@app.post("/api/register")
def register():
    payload = request.get_json(force=True)
    nickname = payload.get("nickname", "").strip()
    try:
        session_token, client = store.register_client(nickname)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"sessionToken": session_token, "client": client, "state": public_state()})


@app.post("/api/entry-status")
def entry_status():
    payload = request.get_json(force=True)
    nickname = payload.get("nickname", "").strip()
    try:
        result = store.inspect_nickname_entry(nickname)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.post("/api/reconnect")
def reconnect():
    payload = request.get_json(force=True)
    nickname = payload.get("nickname", "").strip()
    try:
        session_token, client = store.reconnect_client_by_nickname(nickname)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"sessionToken": session_token, "client": client, "state": public_state()})


@app.post("/api/reset")
def reset():
    store.reset_game()
    broadcast_state()
    return jsonify({"ok": True})


@app.post("/api/manager/settings")
def update_manager_settings():
    payload = request.get_json(force=True) or {}

    raw_round_durations = payload.get("roundDurations")
    round_durations = None
    if raw_round_durations is not None:
        try:
            round_durations = [int(value) for value in raw_round_durations]
        except (TypeError, ValueError):
            return jsonify({"error": "라운드 제한 시간은 숫자 목록이어야 합니다."}), 400

    try:
        store.update_manager_settings(
            round_durations=round_durations,
            join_url=payload.get("joinUrl") if "joinUrl" in payload else None,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return ("", 204)


@app.post("/api/manager/start")
def start_game():
    payload = request.get_json(force=True)
    reference_prompt = payload.get("referencePrompt", "").strip()
    reference_image_url = payload.get("referenceImageUrl", "").strip()
    round_durations = payload.get("roundDurations")

    if not reference_prompt:
        return jsonify({"error": "기준 그림 설명이 필요합니다."}), 400
    manager_reference = {
        "prompt": reference_prompt,
        "image_url": reference_image_url or placeholder_image(reference_prompt, "Manager"),
    }
    try:
        store.start_game(manager_reference, round_durations=round_durations)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    broadcast_state()
    return jsonify({"ok": True, "state": public_state()})


@app.post("/api/manager/advance-round")
def advance_round():
    payload = request.get_json(force=True)
    raw_scores = payload.get("scores") or {}

    try:
        active_team_ids = store.state["game"].get("active_team_ids", [])
        scores = {team_id: int(raw_scores.get(team_id, 0)) for team_id in active_team_ids}
        store.apply_review_scores(scores)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    broadcast_state()
    return jsonify({"ok": True, "state": public_state()})


@app.post("/api/manager/retry-review")
def retry_review():
    review_state = store.get_review_payload()
    if store.state["game"].get("status") != "review" or not review_state:
        return jsonify({"error": "No review round is active."}), 400

    round_number = int(review_state.get("round_number", 0))
    if round_number in store.pending_review_rounds:
        return jsonify({"error": "AI judging is already in progress."}), 409
    submitted_images = review_state.get("submitted_images") or []
    if not submitted_images:
        return jsonify({"error": "제출한 팀이 없어 AI 평가를 건너뜁니다."}), 400
    active_team_ids = [item.get("team_id") for item in submitted_images if item.get("team_id")]

    store.update_review_judge_result(
        {
            "assistant_message": "Retrying AI judging...",
            "similarities": [],
            "scores": [],
            "ranking": [],
            "status": "streaming",
        },
        manager_scores=review_state.get("manager_scores") or {team_id: 0 for team_id in active_team_ids},
    )
    if not launch_judge_review(round_number, review_state.get("reference") or {}, submitted_images, active_team_ids):
        return jsonify({"error": "AI judging is already in progress."}), 409

    broadcast_state()
    return jsonify({"ok": True, "state": public_state()})


@app.post("/api/manager/finish-round")
def finish_round():
    round_state = store.current_round()
    if not round_state or store.state["game"].get("status") != "running":
        return jsonify({"error": "진행 중인 라운드가 없습니다."}), 400

    maybe_finish_round(force=True)
    return jsonify({"ok": True, "state": public_state()})


@socketio.on("connect")
def on_connect():
    emit("state:update", public_state())


@socketio.on("disconnect")
def on_disconnect():
    log_game_event("socket_disconnected", sid=request.sid)
    store.detach_socket(request.sid)
    broadcast_state()


@socketio.on("session:join")
def on_session_join(data):
    role = data.get("role", "client")
    session_token = data.get("sessionToken")

    if role == "manager":
        store.attach_manager(session_token or "manager", request.sid)
        log_game_event("manager_joined", session_token=session_token or "manager", sid=request.sid)
        emit("session:joined", {"role": "manager", "state": public_state()})
        broadcast_state()
        return

    client = store.attach_socket(session_token, request.sid)
    if not client:
        emit("session:error", {"message": "세션을 찾을 수 없습니다. 다시 등록해주세요."})
        return
    log_game_event(
        "client_joined",
        session_token=session_token,
        sid=request.sid,
        nickname=client.get("nickname"),
        name=client.get("name"),
        team_id=client.get("team_id"),
    )
    emit("session:joined", build_client_payload(session_token))
    broadcast_state()


@socketio.on("manager:assign_team")
def on_assign_team(data):

    nickname = data.get("nickname")
    team_id = data.get("teamId")
    if team_id == "__new__":
        team_id = store.create_next_team()
    elif team_id and team_id not in store.lobby_team_ids():
        emit("session:error", {"message": "잘못된 팀 ID입니다."})
        return

    store.assign_team(nickname, team_id)
    broadcast_state()


@socketio.on("manager:add_test_bot")
def on_add_test_bot(data):

    team_id = data.get("teamId")
    if not team_id or team_id not in store.lobby_team_ids():
        emit("session:error", {"message": "잘못된 팀 ID입니다."})
        return

    client, stop_event = store.create_test_bot(team_id)
    log_game_event(
        "test_bot_added",
        session_token=client["session_token"],
        nickname=client["nickname"],
        name=client["name"],
        team_id=team_id,
    )
    threading.Thread(target=run_test_bot_loop, args=(client["session_token"], stop_event), daemon=True).start()
    broadcast_state()


@socketio.on("team:add_note")
def on_add_note(data):
    client = store.get_client(data.get("sessionToken"))
    if not client or not client.get("team_id"):
        emit("session:error", {"message": "팀 배정 후 사용할 수 있습니다."})
        return
    if store.state["game"]["status"] != "running":
        emit("session:error", {"message": "플레이 라운드에서만 입력할 수 있습니다."})
        return

    text = data.get("text", "").strip()
    if not text:
        emit("session:error", {"message": "메모를 입력해주세요."})
        return

    try:
        store.add_note(client["team_id"], text, client["name"])
    except ValueError as exc:
        emit("session:error", {"message": str(exc)})
        return
    broadcast_state()


@socketio.on("team:delete_note")
def on_delete_note(data):
    client = store.get_client(data.get("sessionToken"))
    if not client or not client.get("team_id"):
        emit("session:error", {"message": "팀 배정 후 사용할 수 있습니다."})
        return
    if store.state["game"]["status"] != "running":
        emit("session:error", {"message": "플레이 라운드에서만 삭제할 수 있습니다."})
        return

    try:
        store.delete_note(client["team_id"], data.get("noteId"))
    except ValueError as exc:
        emit("session:error", {"message": str(exc)})
        return

    broadcast_state()


@socketio.on("team:generate_image")
def on_generate_image(data):
    return handle_team_generate_image(data)


def handle_team_generate_image(data):
    client = store.get_client(data.get("sessionToken"))
    if not client or not client.get("team_id"):
        emit("session:error", {"message": "팀 배정 후 사용할 수 있습니다."})
        return

    round_state = store.current_round()
    if not round_state or round_state["status"] != "running":
        emit("session:error", {"message": "진행 중인 라운드가 없습니다."})
        return

    team_id = client["team_id"]
    round_number = round_state["round_number"]
    team_state = round_state["teams"][team_id]
    if team_state["submitted"]:
        emit("session:error", {"message": "이미 제출한 라운드입니다."})
        return
    if not store.start_team_generation(round_number, team_id):
        emit("session:error", {"message": "현재 우리 팀의 이미지 생성이 이미 진행 중입니다."})
        return
    if team_state["generations_used"] >= MAX_GENERATIONS:
        store.finish_team_generation(round_number, team_id)
        broadcast_state()
        emit("session:error", {"message": f"이미지 생성은 팀당 {MAX_GENERATIONS}회까지 가능합니다."})
        return

    prompt = data.get("prompt", "").strip()
    if not prompt:
        prompt = "\n".join(
            note["text"].strip() for note in team_state.get("notes", []) if note.get("text", "").strip()
        )
    if not prompt:
        emit("session:error", {"message": "팀 프롬프트를 먼저 입력하거나 공유해주세요."})
        return

    if not prompt:
        store.finish_team_generation(round_number, team_id)
        store.finish_team_generation(round_number, team_id)
        emit("session:error", {"message": "팀 프롬프트를 먼저 입력하거나 공유해주세요."})
        return

    prompt_debug_payload = {
        "event": "team:generate_image",
        "round_number": round_number,
        "team_id": team_id,
        "nickname": client["nickname"],
        "name": client["name"],
        "shared_notes": [
            {
                "id": note.get("id"),
                "text": note.get("text", ""),
                "author": note.get("author"),
                "created_at": note.get("created_at"),
            }
            for note in team_state.get("notes", [])
        ],
        "request_prompt": data.get("prompt", ""),
        "resolved_prompt": prompt,
    }
    print(json.dumps(prompt_debug_payload, ensure_ascii=False, indent=2), flush=True)

    socketio.start_background_task(
        run_team_image_generation_async,
        request_sid=request.sid,
        round_number=round_number,
        team_id=team_id,
        prompt=prompt,
    )
    return


def handle_team_generate_image(data):
    client = store.get_client(data.get("sessionToken"))
    if not client or not client.get("team_id"):
        emit("session:error", {"message": "팀 배정된 사용자만 이미지를 생성할 수 있습니다."})
        return

    round_state = store.current_round()
    if not round_state or round_state["status"] != "running":
        emit("session:error", {"message": "진행 중인 라운드가 없습니다."})
        return

    team_id = client["team_id"]
    round_number = round_state["round_number"]
    team_state = round_state["teams"][team_id]

    if team_state["submitted"]:
        emit("session:error", {"message": "이미 제출한 라운드입니다."})
        return
    if team_state["generations_used"] >= MAX_GENERATIONS:
        emit("session:error", {"message": f"이미지 생성은 팀당 {MAX_GENERATIONS}회까지 가능합니다."})
        return
    if not store.start_team_generation(round_number, team_id):
        emit("session:error", {"message": "현재 우리 팀의 이미지 생성이 이미 진행 중입니다."})
        return

    broadcast_state()

    prompt = data.get("prompt", "").strip()
    if not prompt:
        prompt = "\n".join(
            note["text"].strip() for note in team_state.get("notes", []) if note.get("text", "").strip()
        )
    if not prompt:
        store.finish_team_generation(round_number, team_id)
        emit("session:error", {"message": "팀 프롬프트를 먼저 입력하거나 공유해주세요."})
        return

    log_game_event(
        "image_generation_requested",
        round_number=round_number,
        team_id=team_id,
        nickname=client["nickname"],
        name=client["name"],
        prompt=summarize_text(prompt),
        prompt_source="manual" if data.get("prompt", "").strip() else "notes",
    )

    prompt_debug_payload = {
        "event": "team:generate_image",
        "round_number": round_number,
        "team_id": team_id,
        "nickname": client["nickname"],
        "name": client["name"],
        "shared_notes": [
            {
                "id": note.get("id"),
                "text": note.get("text", ""),
                "author": note.get("author"),
                "created_at": note.get("created_at"),
            }
            for note in team_state.get("notes", [])
        ],
        "request_prompt": data.get("prompt", ""),
        "resolved_prompt": prompt,
    }
    print(json.dumps(prompt_debug_payload, ensure_ascii=False, indent=2), flush=True)

    socketio.start_background_task(
        run_team_image_generation_async,
        request_sid=request.sid,
        round_number=round_number,
        team_id=team_id,
        prompt=prompt,
    )


@socketio.on("team:submit_image")
def on_submit_image(data):
    client = store.get_client(data.get("sessionToken"))
    if not client or not client.get("team_id"):
        emit("session:error", {"message": "팀 배정 후 사용할 수 있습니다."})
        return
    if store.state["game"]["status"] != "running":
        emit("session:error", {"message": "플레이 라운드에서만 제출할 수 있습니다."})
        return

    try:
        store.submit_image(client["team_id"], data.get("imageId"))
    except ValueError as exc:
        emit("session:error", {"message": str(exc)})
        return

    broadcast_state()
    maybe_finish_round()


@socketio.on("team:select_image")
def on_select_image(data):
    client = store.get_client(data.get("sessionToken"))
    if not client or not client.get("team_id"):
        emit("session:error", {"message": "팀 배정 후 사용할 수 있습니다."})
        return
    if store.state["game"]["status"] != "running":
        emit("session:error", {"message": "플레이 라운드에서만 이미지를 선택할 수 있습니다."})
        return

    try:
        store.select_image(client["team_id"], data.get("imageId"))
    except ValueError as exc:
        emit("session:error", {"message": str(exc)})
        return

    broadcast_state()


@app.get("/generated/<path:name>")
def generated_file(name):
    media = store.get_generated_media(name)
    if media:
        return Response(media["bytes"], mimetype=media["mime_type"])
    abort(404)


if __name__ == "__main__":
    threading.Thread(target=timer_loop, daemon=True).start()
    resolved_port = int(os.environ.get("PORT", "5000"))
    debug_enabled = os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true", "yes", "on"}
    print(f"Running on http://127.0.0.1:{resolved_port}", flush=True)
    print(f"Running on http://0.0.0.0:{resolved_port}", flush=True)
    log_game_event(
        "server_started",
        host="0.0.0.0",
        port=resolved_port,
        debug=debug_enabled,
    )
    socketio.run(
        app,
        host="0.0.0.0",
        port=resolved_port,
        debug=debug_enabled,
    )

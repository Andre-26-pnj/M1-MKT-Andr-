import json
import os
import random
import string
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-me")

QUESTION_SECONDS = 20
REVEAL_SECONDS = 6
ROOM_TTL_SECONDS = 2 * 60 * 60  # 2 hours
PLAYER_STALE_SECONDS = 15


def _now() -> float:
    return time.time()


def _safe_upper(s: str) -> str:
    return (s or "").strip()


def _generate_room_code() -> str:
    # Example: QUIZ-X7K2A9 (6 chars after dash)
    suffix = "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    return f"QUIZ-{suffix}"


def _get_player_id() -> str:
    pid = session.get("player_id")
    if not pid:
        pid = str(uuid.uuid4())
        session["player_id"] = pid
    return pid


def _is_connected(last_seen: float) -> bool:
    return (_now() - last_seen) <= PLAYER_STALE_SECONDS


def _score_for_elapsed(elapsed_s: float) -> int:
    elapsed_s = max(0.0, min(float(elapsed_s), float(QUESTION_SECONDS)))
    return int(round(1000 * (1.0 - (elapsed_s / float(QUESTION_SECONDS)))))


def _fun_title(score: int, max_score: int) -> str:
    if max_score <= 0:
        return "Mystère ambulant"
    ratio = score / max_score
    if ratio >= 0.9:
        return "Génie incompris 🧠✨"
    if ratio >= 0.75:
        return "Machine à bonnes réponses 🤖✅"
    if ratio >= 0.55:
        return "Héros du dimanche 🦸‍♂️"
    if ratio >= 0.35:
        return "Chançard professionnel 🍀"
    if ratio >= 0.15:
        return "Brave mais pas brillant 🫡"
    return "Légende urbaine (dans le mauvais sens) 🫠"


@dataclass
class Player:
    id: str
    name: str
    joined_at: float
    last_seen: float
    score: int = 0


@dataclass
class Room:
    code: str
    creator_id: str
    theme: str
    difficulty: str
    num_questions: int
    created_at: float
    updated_at: float
    status: str = "lobby"  # lobby | playing | finished
    players: Dict[str, Player] = field(default_factory=dict)
    questions: List[Dict[str, Any]] = field(default_factory=list)
    current_index: int = 0
    phase: str = "lobby"  # lobby | question | reveal | finished
    question_started_at: Optional[float] = None
    reveal_started_at: Optional[float] = None
    # answers[(q_index, player_id)] = {"choice": "A", "is_correct": bool, "score": int, "answered_at": ts}
    answers: Dict[Tuple[int, str], Dict[str, Any]] = field(default_factory=dict)
    # correct cache per question index for quick checks
    correct_by_index: Dict[int, str] = field(default_factory=dict)


ROOMS: Dict[str, Room] = {}
ROOMS_LOCK = threading.Lock()


def _cleanup_loop() -> None:
    while True:
        time.sleep(60)
        cutoff = _now() - ROOM_TTL_SECONDS
        with ROOMS_LOCK:
            stale = [code for code, room in ROOMS.items() if room.updated_at < cutoff]
            for code in stale:
                ROOMS.pop(code, None)


threading.Thread(target=_cleanup_loop, daemon=True).start()


def _get_room_or_404(code: str) -> Room:
    with ROOMS_LOCK:
        room = ROOMS.get(code)
        if not room:
            abort(404)
        return room


def _touch_room(room: Room) -> None:
    room.updated_at = _now()


def _ensure_player_in_room(room: Room, player_id: str) -> Player:
    if player_id not in room.players:
        abort(403)
    return room.players[player_id]


def _rankings(room: Room) -> List[Dict[str, Any]]:
    players = list(room.players.values())
    players.sort(key=lambda p: (-p.score, p.joined_at))
    return [
        {
            "id": p.id,
            "name": p.name,
            "score": p.score,
            "connected": _is_connected(p.last_seen),
        }
        for p in players
    ]


def _current_question_payload(room: Room) -> Optional[Dict[str, Any]]:
    if not room.questions or room.current_index >= len(room.questions):
        return None
    q = room.questions[room.current_index]
    return {
        "index": room.current_index,
        "total": len(room.questions),
        "question": q.get("question"),
        "choices": q.get("choices", {}),
    }


def _all_players_answered(room: Room, q_index: int) -> bool:
    for pid in room.players.keys():
        if (q_index, pid) not in room.answers:
            return False
    return True


def _advance_state_if_needed(room: Room) -> None:
    if room.phase == "question":
        if room.question_started_at is None:
            room.question_started_at = _now()
        elapsed = _now() - room.question_started_at
        if elapsed >= QUESTION_SECONDS or _all_players_answered(room, room.current_index):
            room.phase = "reveal"
            room.reveal_started_at = _now()
    elif room.phase == "reveal":
        if room.reveal_started_at is None:
            room.reveal_started_at = _now()
        if (_now() - room.reveal_started_at) >= REVEAL_SECONDS:
            room.current_index += 1
            room.question_started_at = None
            room.reveal_started_at = None
            if room.current_index >= len(room.questions):
                room.phase = "finished"
                room.status = "finished"
            else:
                room.phase = "question"


def _anthropic_client():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        from anthropic import Anthropic
    except Exception:
        return None
    return Anthropic(api_key=api_key)


def _fallback_questions(theme: str, difficulty: str, n: int) -> List[Dict[str, Any]]:
    # Fun, safe defaults so the app works without API key.
    base = [
        {
            "question": "Quel animal peut dormir debout sans tomber (et a une tête de 'je fais semblant d’écouter') ?",
            "choices": {"A": "Le cheval", "B": "Le pingouin", "C": "Le lama", "D": "Le poisson rouge"},
            "answer": "A",
            "anecdote": "Les chevaux ont un 'verrou' dans les jambes. Toi, tu as juste un verrou sur le frigo.",
        },
        {
            "question": "Si on empile des spaghettis jusqu’à la Lune, que se passe-t-il le plus probablement ?",
            "choices": {
                "A": "La Lune dit merci",
                "B": "Ça casse avant d’arriver (spoiler)",
                "C": "On découvre un nouveau continent",
                "D": "Les spaghettis deviennent administratifs",
            },
            "answer": "B",
            "anecdote": "La rigidité des pâtes, c’est comme ta motivation: ça tient… puis ça plie.",
        },
        {
            "question": "Quel objet a été inventé avant le briquet ?",
            "choices": {"A": "Les allumettes", "B": "Le frigo", "C": "Le briquet", "D": "Le volcan portable"},
            "answer": "C",
            "anecdote": "Oui, le briquet est plus vieux que les allumettes. L’Histoire adore troller.",
        },
        {
            "question": "Quelle planète sent (probablement) le plus les œufs pourris ?",
            "choices": {"A": "Mars", "B": "Vénus", "C": "Jupiter", "D": "Saturne"},
            "answer": "B",
            "anecdote": "Vénus = dioxyde de soufre. Le brunch cosmique n’est pas recommandé.",
        },
        {
            "question": "Quel sport a le plus de chances d’impliquer une plume au mauvais endroit ?",
            "choices": {"A": "Badminton", "B": "Boxe", "C": "Plongée", "D": "Échecs aquatiques"},
            "answer": "A",
            "anecdote": "Le volant vole, et parfois ton ego aussi.",
        },
    ]
    random.shuffle(base)
    out = base[: max(1, min(n, len(base)))]
    # Lightly theme it in UI copy only; keep as-is for stability.
    return out


def _generate_questions(theme: str, difficulty: str, n: int) -> List[Dict[str, Any]]:
    client = _anthropic_client()
    if not client:
        return _fallback_questions(theme, difficulty, n)

    system = (
        "Tu es un générateur de quiz fun et drôle en français. "
        "Tu dois répondre UNIQUEMENT avec du JSON valide, sans texte autour."
    )
    user = {
        "theme": theme,
        "difficulty": difficulty,
        "count": n,
        "format": {
            "questions": [
                {
                    "question": "string",
                    "choices": {"A": "string", "B": "string", "C": "string", "D": "string"},
                    "answer": "A|B|C|D",
                    "anecdote": "string (courte, humoristique)",
                }
            ]
        },
        "constraints": [
            "Questions adaptées à des amis (pas de contenu choquant).",
            "Une seule bonne réponse.",
            "Humour léger, pas insultant.",
            "Anecdote max 140 caractères.",
        ],
    }

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1600,
            temperature=0.8,
            system=system,
            messages=[{"role": "user", "content": json.dumps(user, ensure_ascii=False)}],
        )
        # anthropic sdk returns list of content blocks
        text = ""
        for block in msg.content:
            if getattr(block, "type", None) == "text":
                text += block.text
        data = json.loads(text)
        questions = data.get("questions") if isinstance(data, dict) else None
        if not isinstance(questions, list) or not questions:
            raise ValueError("Invalid questions format")
        normalized = []
        for q in questions[:n]:
            if not isinstance(q, dict):
                continue
            choices = q.get("choices") or {}
            if not all(k in choices for k in ["A", "B", "C", "D"]):
                continue
            ans = (q.get("answer") or "").strip().upper()
            if ans not in ["A", "B", "C", "D"]:
                continue
            normalized.append(
                {
                    "question": str(q.get("question", "")).strip(),
                    "choices": {k: str(choices[k]).strip() for k in ["A", "B", "C", "D"]},
                    "answer": ans,
                    "anecdote": str(q.get("anecdote", "")).strip(),
                }
            )
        if len(normalized) < max(1, n // 2):
            raise ValueError("Too few valid questions parsed")
        return normalized[:n]
    except Exception:
        return _fallback_questions(theme, difficulty, n)


@app.get("/")
def index():
    prefill_code = request.args.get("code", "")
    return render_template("index.html", prefill_code=prefill_code)


@app.post("/create")
def create():
    name = _safe_upper(request.form.get("creator_name"))
    theme = _safe_upper(request.form.get("theme"))
    difficulty = _safe_upper(request.form.get("difficulty"))
    try:
        num_questions = int(request.form.get("num_questions", "10"))
    except Exception:
        num_questions = 10

    if not name:
        return redirect(url_for("index"))
    if num_questions not in [5, 10, 15]:
        num_questions = 10
    if difficulty not in ["Facile", "Moyen", "Difficile"]:
        difficulty = "Moyen"

    player_id = _get_player_id()
    with ROOMS_LOCK:
        code = _generate_room_code()
        while code in ROOMS:
            code = _generate_room_code()
        room = Room(
            code=code,
            creator_id=player_id,
            theme=theme or "Culture générale",
            difficulty=difficulty,
            num_questions=num_questions,
            created_at=_now(),
            updated_at=_now(),
        )
        room.players[player_id] = Player(
            id=player_id, name=name, joined_at=_now(), last_seen=_now(), score=0
        )
        ROOMS[code] = room

    return redirect(url_for("room", code=code))


@app.post("/join")
def join():
    code = _safe_upper(request.form.get("code")).upper()
    name = _safe_upper(request.form.get("player_name"))
    if not code:
        return redirect(url_for("index"))
    if not name:
        return redirect(url_for("index", code=code))

    player_id = _get_player_id()
    with ROOMS_LOCK:
        room = ROOMS.get(code)
        if not room:
            return redirect(url_for("index", code=code))
        # If same session already present, just update name (handy on mobile refresh)
        if player_id in room.players:
            room.players[player_id].name = name
            room.players[player_id].last_seen = _now()
        else:
            room.players[player_id] = Player(
                id=player_id, name=name, joined_at=_now(), last_seen=_now(), score=0
            )
        _touch_room(room)

    return redirect(url_for("room", code=code))


@app.get("/join/<code>")
def join_link(code: str):
    return redirect(url_for("index", code=_safe_upper(code).upper()))


@app.get("/room/<code>")
def room(code: str):
    code = _safe_upper(code).upper()
    room = _get_room_or_404(code)
    player_id = _get_player_id()
    with ROOMS_LOCK:
        if player_id in room.players:
            room.players[player_id].last_seen = _now()
            _touch_room(room)
        phase = room.phase

    if phase == "lobby":
        return render_template("lobby.html", code=code)
    if phase in ["question", "reveal"]:
        return render_template("game.html", code=code)
    return render_template("results.html", code=code)


@app.get("/api/room/<code>/state")
def api_state(code: str):
    code = _safe_upper(code).upper()
    player_id = _get_player_id()
    room = _get_room_or_404(code)

    with ROOMS_LOCK:
        if player_id in room.players:
            room.players[player_id].last_seen = _now()
            _touch_room(room)

        # server-side progression (driven by polling)
        if room.phase in ["question", "reveal"]:
            _advance_state_if_needed(room)

        me = room.players.get(player_id)
        rankings = _rankings(room)
        connected_count = sum(1 for p in room.players.values() if _is_connected(p.last_seen))
        share_url = request.url_root.rstrip("/") + url_for("join_link", code=code)

        payload: Dict[str, Any] = {
            "code": room.code,
            "phase": room.phase,
            "status": room.status,
            "theme": room.theme,
            "difficulty": room.difficulty,
            "num_questions": room.num_questions,
            "share_url": share_url,
            "players": rankings,
            "connected_count": connected_count,
            "me": {
                "id": me.id if me else None,
                "name": me.name if me else None,
                "score": me.score if me else 0,
                "is_creator": (me.id == room.creator_id) if me else False,
            },
        }

        if room.phase == "lobby":
            payload["can_start"] = len(room.players) >= 2 and (player_id == room.creator_id)
        elif room.phase in ["question", "reveal"]:
            q_payload = _current_question_payload(room)
            payload["question"] = q_payload
            payload["question_seconds"] = QUESTION_SECONDS
            payload["reveal_seconds"] = REVEAL_SECONDS
            payload["question_started_at"] = room.question_started_at
            payload["reveal_started_at"] = room.reveal_started_at

            answered = (room.current_index, player_id) in room.answers
            payload["me"]["has_answered"] = answered
            if room.phase == "reveal":
                q = room.questions[room.current_index] if q_payload else None
                payload["reveal"] = {
                    "correct": q.get("answer") if q else None,
                    "anecdote": q.get("anecdote") if q else "",
                }
        elif room.phase == "finished":
            max_score = 1000 * max(1, len(room.questions))
            payload["final"] = {
                "max_score": max_score,
            }
            if me:
                payload["final"]["title"] = _fun_title(me.score, max_score)

        return jsonify(payload)


@app.post("/api/room/<code>/start")
def api_start(code: str):
    code = _safe_upper(code).upper()
    player_id = _get_player_id()
    room = _get_room_or_404(code)

    with ROOMS_LOCK:
        _ensure_player_in_room(room, player_id)
        if player_id != room.creator_id:
            abort(403)
        if room.phase != "lobby":
            return jsonify({"ok": True})
        if len(room.players) < 2:
            return jsonify({"ok": False, "error": "Au moins 2 joueurs requis."}), 400

        room.questions = _generate_questions(room.theme, room.difficulty, room.num_questions)
        room.correct_by_index = {i: q["answer"] for i, q in enumerate(room.questions)}
        room.current_index = 0
        room.answers = {}
        room.status = "playing"
        room.phase = "question"
        room.question_started_at = _now()
        room.reveal_started_at = None
        _touch_room(room)

    return jsonify({"ok": True})


@app.post("/api/room/<code>/answer")
def api_answer(code: str):
    code = _safe_upper(code).upper()
    player_id = _get_player_id()
    room = _get_room_or_404(code)
    data = request.get_json(silent=True) or {}
    choice = str(data.get("choice", "")).strip().upper()

    if choice not in ["A", "B", "C", "D"]:
        return jsonify({"ok": False, "error": "Choix invalide."}), 400

    with ROOMS_LOCK:
        me = _ensure_player_in_room(room, player_id)
        if room.phase != "question":
            return jsonify({"ok": False, "error": "Trop tard 🙂"}), 400
        if room.question_started_at is None:
            room.question_started_at = _now()

        key = (room.current_index, player_id)
        if key in room.answers:
            return jsonify({"ok": True, "already": True})

        elapsed = _now() - room.question_started_at
        is_correct = choice == room.correct_by_index.get(room.current_index)
        gained = _score_for_elapsed(elapsed) if is_correct and elapsed <= QUESTION_SECONDS else 0
        me.score += gained
        room.answers[key] = {
            "choice": choice,
            "is_correct": is_correct,
            "score": gained,
            "answered_at": _now(),
        }
        _touch_room(room)

        # progress faster if everyone answered
        _advance_state_if_needed(room)

    return jsonify({"ok": True, "correct": is_correct, "gained": gained})


@app.post("/api/room/<code>/replay")
def api_replay(code: str):
    code = _safe_upper(code).upper()
    player_id = _get_player_id()
    room = _get_room_or_404(code)

    with ROOMS_LOCK:
        _ensure_player_in_room(room, player_id)
        if room.phase != "finished":
            return jsonify({"ok": False, "error": "La partie n'est pas terminée."}), 400

        new_code = _generate_room_code()
        while new_code in ROOMS:
            new_code = _generate_room_code()

        new_room = Room(
            code=new_code,
            creator_id=player_id,
            theme=room.theme,
            difficulty=room.difficulty,
            num_questions=room.num_questions,
            created_at=_now(),
            updated_at=_now(),
        )
        # Only add the requester to avoid "ghost players".
        me = room.players[player_id]
        new_room.players[player_id] = Player(
            id=player_id, name=me.name, joined_at=_now(), last_seen=_now(), score=0
        )
        ROOMS[new_code] = new_room

    return jsonify({"ok": True, "code": new_code, "url": url_for("room", code=new_code)})


@app.post("/api/room/<code>/leave")
def api_leave(code: str):
    code = _safe_upper(code).upper()
    player_id = _get_player_id()
    room = _get_room_or_404(code)
    with ROOMS_LOCK:
        if player_id in room.players:
            room.players[player_id].last_seen = 0  # mark disconnected; keep score
            _touch_room(room)
    return jsonify({"ok": True})


@app.errorhandler(404)
def not_found(_e):
    return render_template("index.html", prefill_code="", error="Salle introuvable 😅"), 404


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)


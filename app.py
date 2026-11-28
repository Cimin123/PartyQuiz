import csv
import io
import json
import math
import os
import random
import secrets
import socket
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
MAX_BODY_SIZE = 1_000_000
AUTO_ADVANCE_SECONDS = 3
ROOM_SNAPSHOT_INTERVAL_SECONDS = 3 * 60
ROOM_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
PLAYER_REJOIN_CODE_LENGTH = 8
SNAPSHOT_VERSION = 2
ROOMS = {}
ROOMS_LOCK = threading.RLock()
SNAPSHOT_DIR = ROOT / "snapshots"


class RequestError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class QuestionModifier:
    key: str
    name: str
    instruction: str
    time_limit: int | None = None
    correct_points: int = 1
    wrong_penalty: int = 0

    EVERY_QUESTIONS: ClassVar[int] = 5
    ALL: ClassVar[tuple["QuestionModifier", ...]]

    @classmethod
    def for_question(cls, question_number):
        if question_number % cls.EVERY_QUESTIONS:
            return None
        return random.choice(cls.ALL)

    def public_data(self):
        return {
            "key": self.key,
            "name": self.name,
            "instruction": self.instruction,
            "time_limit": self.time_limit,
        }


QuestionModifier.ALL = (
    QuestionModifier(
        "team_toast",
        "Przybij piątkę!",
        "Przed odpowiedzią przybij piątkę z solenizantem.",
    ),
    QuestionModifier(
        "no_talking",
        "Bez gadania",
        "Nie wolno rozmawiać podczas odpowiadania na pytanie.",
    ),
    QuestionModifier(
        "captain",
        "Kapitan decyduje",
        "Tylko osoba trzymająca telefon albo siedząca najbliżej niego może odpowiadać w tej rundzie.",
    ),
    QuestionModifier(
        "opposing_team_compliment",
        "Komplement dla rywali",
        "Przed odpowiedzią skomplementujcie osobę z przeciwnej drużyny.",
    ),
    QuestionModifier(
        "consult_paulina",
        "Konsultacje z Pauliną",
        "Skonsultujcie odpowiedź z Pauliną, zanim ją wybierzecie.",
    ),
    QuestionModifier(
        "team_treat",
        "Poczęstunek dla zwycięzców",
        "Za zgodą wszystkich: drużyny z błędną odpowiedzią stawiają tym z poprawną szocika",
    ),
    QuestionModifier(
        "speed_round",
        "Szybko!",
        "Macie tylko 15 sekund na odpowiedź!",
        time_limit=15,
    ),
    QuestionModifier(
        "double_points",
        "Podwójne punkty",
        "Poprawna odpowiedź daje 3 punkty.",
        correct_points=3,
    ),
    QuestionModifier(
        "wrong_answer_penalty",
        "Kara jak w kangurku",
        "Błędna odpowiedź odejmuje dwa punkty.",
        wrong_penalty=2,
    ),
)

SECOND_CHANCE_WINDOW_SECONDS = 5
PLAYER_POWERUPS = (
    {
        "key": "fifty_fifty",
        "name": "50:50",
        "description": "Hide half of the answer choices (all incorrect) for yourself.",
    },
    {
        "key": "scout",
        "name": "Scout",
        "description": "See an anonymous count of other players' answers to this question.",
    },
    {
        "key": "call_friend",
        "name": "Call a friend",
        "description": "Ask someone outside the game for help before answering.",
    },
    {
        "key": "second_chance",
        "name": "Second chance",
        "description": "If your first guess is wrong, you can make one different guess before the reveal.",
    },
    {
        "key": "hide_answers",
        "name": "Memory test",
        "description": "Hide the answer text for all other eligible players until the reveal.",
    },
)
PLAYER_POWERUP_KEYS = {powerup["key"] for powerup in PLAYER_POWERUPS}


def validate_quiz(data):
    if not isinstance(data, dict):
        raise RequestError("Quiz must be a JSON object.")

    title = data.get("title", "Party Quiz")
    questions = data.get("questions")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 100:
        raise RequestError("Quiz title must be between 1 and 100 characters.")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 100:
        raise RequestError("Quiz must contain between 1 and 100 questions.")

    validated_questions = []
    for question_number, question in enumerate(questions, start=1):
        if not isinstance(question, dict):
            raise RequestError("Question {} must be an object.".format(question_number))

        prompt = question.get("question")
        category = question.get("category")
        answers = question.get("answers")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.strip()) > 500:
            raise RequestError(
                "Question {} must have text between 1 and 500 characters.".format(
                    question_number
                )
            )
        if category is not None and (
            not isinstance(category, str)
            or len(category.strip()) > 100
        ):
            raise RequestError(
                "Category in question {} must be at most 100 characters.".format(
                    question_number
                )
            )
        if not isinstance(answers, list) or len(answers) > 6:
            raise RequestError(
                "Question {} must have between 2 and 6 answers.".format(question_number)
            )

        validated_answers = []
        correct_count = 0
        for answer in answers:
            if not isinstance(answer, dict):
                raise RequestError(
                    "Answers in question {} must be objects.".format(question_number)
                )
            text = answer.get("text")
            correct = answer.get("correct", False)
            if not isinstance(correct, bool):
                raise RequestError(
                    "The correct field in question {} must be true or false.".format(
                        question_number
                    )
                )
            if not isinstance(text, str):
                raise RequestError(
                    "Every answer in question {} must have text between 1 and 200 characters.".format(
                        question_number
                    )
                )
            if not text.strip():
                if correct:
                    raise RequestError(
                        "The correct answer in question {} cannot be blank.".format(
                            question_number
                        )
                    )
                continue
            if len(text.strip()) > 200:
                raise RequestError(
                    "Every answer in question {} must have text between 1 and 200 characters.".format(
                        question_number
                    )
                )
            correct_count += int(correct)
            validated_answers.append({"text": text.strip(), "correct": correct})

        if not 2 <= len(validated_answers) <= 6:
            raise RequestError(
                "Question {} must have between 2 and 6 answers.".format(question_number)
            )
        if correct_count != 1:
            raise RequestError(
                "Question {} must have exactly one answer marked correct.".format(
                    question_number
                )
            )
        validated_question = {
            "question": prompt.strip(),
            "answers": validated_answers,
        }
        if category and category.strip():
            validated_question["category"] = category.strip()
        validated_questions.append(validated_question)

    return {"title": title.strip(), "questions": validated_questions}


def parse_csv_quiz(content, title):
    if not isinstance(content, str) or not content.strip():
        raise RequestError("CSV file is empty.")

    sample = content[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")), dialect=dialect)
    if not reader.fieldnames:
        raise RequestError("CSV file must include a header row.")

    def normalize_header(header):
        return " ".join(unicodedata.normalize("NFKC", header).casefold().split())

    headers = {}
    for header in reader.fieldnames:
        normalized = normalize_header(header or "")
        if not normalized:
            continue
        if normalized in headers:
            raise RequestError("CSV contains duplicate column headers.")
        headers[normalized] = header

    question_header = next(
        (headers[name] for name in ("pytanie", "question") if name in headers),
        None,
    )
    correct_header = next(
        (
            headers[name]
            for name in (
                "poprawna odp",
                "poprawna odpowiedź",
                "correct answer",
                "correct",
            )
            if name in headers
        ),
        None,
    )
    category_header = next(
        (headers[name] for name in ("kategoria", "category") if name in headers),
        None,
    )
    answer_headers = [
        headers[letter]
        for letter in "abcde"
        if letter in headers
    ]
    include_header = headers.get("include")

    if question_header is None or correct_header is None:
        raise RequestError(
            "CSV needs a Question/Pytanie column and a Correct/Poprawna odp column."
        )
    if len(answer_headers) < 2:
        raise RequestError("CSV needs at least two answer columns named A, B, C, D, or E.")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 100:
        raise RequestError("Quiz title must be between 1 and 100 characters.")

    questions = []
    warnings = []
    try:
        rows = list(reader)
    except csv.Error as error:
        raise RequestError("CSV could not be parsed: {}".format(error))

    for row_number, row in enumerate(rows, start=2):
        if None in row:
            warnings.append(
                "CSV row {} skipped: it has more values than the header.".format(
                    row_number
                )
            )
            continue

        values = [value for value in row.values() if value is not None]
        if not any(value.strip() for value in values):
            continue

        if include_header is not None:
            include_value = (row.get(include_header) or "").strip().casefold()
            if include_value in ("0", "false", "no", "n", "nie"):
                continue

        prompt = (row.get(question_header) or "").strip()
        if not prompt:
            warnings.append(
                "CSV row {} skipped: the question is blank.".format(row_number)
            )
            continue

        correct_text = (row.get(correct_header) or "").strip()
        if not correct_text:
            warnings.append(
                "CSV row {} skipped: the correct answer is blank.".format(row_number)
            )
            continue

        answer_texts = []
        for header in answer_headers:
            answer = (row.get(header) or "").strip()
            if answer:
                answer_texts.append(answer)
        matches = [
            index
            for index, answer in enumerate(answer_texts)
            if answer.casefold() == correct_text.casefold()
        ]
        if len(matches) != 1:
            warnings.append(
                "CSV row {} skipped: the correct answer must match exactly one answer choice.".format(
                    row_number
                )
            )
            continue

        question = {
            "question": prompt,
            "answers": [
                {"text": answer, "correct": index == matches[0]}
                for index, answer in enumerate(answer_texts)
            ],
        }
        category = (row.get(category_header) or "").strip() if category_header else ""
        if category:
            question["category"] = category
        try:
            validated = validate_quiz(
                {"title": title.strip(), "questions": [question]}
            )
        except RequestError as error:
            warnings.append(
                "CSV row {} skipped: {}.".format(row_number, error)
            )
            continue
        questions.extend(validated["questions"])

    if not questions:
        details = " ".join(warnings[:3])
        if len(warnings) > 3:
            details += " And {} more row(s) were skipped.".format(len(warnings) - 3)
        message = "CSV contains no valid questions."
        if details:
            message += " " + details
        raise RequestError(message)

    return {"title": title.strip(), "questions": questions}, warnings


class QuizRoom:
    def __init__(self, code, quiz, host_token, mode="manual"):
        self.code = code
        self.quiz = quiz
        self.host_token = host_token
        self.mode = mode
        self.players = {}
        self.phase = "lobby"
        self.question_index = -1
        self.answers = {}
        self.second_chance_deadline = None
        self.modifier = None
        self.question_started_at = None
        self.lock = threading.RLock()

    def public_state(self, player_id=None, include_reconnect_codes=False):
        with self.lock:
            question = None
            correct_answer = None
            correct_answer_index = None
            hide_answers = (
                player_id in self.players
                and self.phase == "question"
                and self.players[player_id]["hide_answers"]
            )
            if self.phase in ("question", "revealed"):
                current = self.quiz["questions"][self.question_index]
                question = {
                    "number": self.question_index + 1,
                    "total": len(self.quiz["questions"]),
                    "text": current["question"],
                    "answers": (
                        [None] * len(current["answers"])
                        if hide_answers
                        else [answer["text"] for answer in current["answers"]]
                    ),
                }
                if current.get("category"):
                    question["category"] = current["category"]
                if self.modifier is not None:
                    question["modifier"] = self.modifier.public_data()
                    if (
                        self.modifier.time_limit is not None
                        and self.phase == "question"
                    ):
                        elapsed = time.monotonic() - self.question_started_at
                        question["time_remaining"] = max(
                            0,
                            math.ceil(self.modifier.time_limit - elapsed),
                        )
                if self.phase == "revealed":
                    correct_answer_index = next(
                        index
                        for index, answer in enumerate(current["answers"])
                        if answer["correct"]
                    )
                    correct_answer = current["answers"][correct_answer_index]["text"]

            players = []
            for current_player_id, player in self.players.items():
                player_state = {
                    "name": player["name"],
                    "score": player["score"],
                    "has_answered": (
                        current_player_id in self.answers
                        and not player["second_chance_wrong_answer"]
                    ),
                }
                if include_reconnect_codes:
                    player_state["reconnect_code"] = player["reconnect_code"]
                players.append(player_state)
            response = {
                "room_code": self.code,
                "title": self.quiz["title"],
                "phase": self.phase,
                "mode": self.mode,
                "question": question,
                "correct_answer": correct_answer,
                "correct_answer_index": correct_answer_index,
                "players": players,
                "player_count": len(self.players),
            }
            if self.second_chance_deadline is not None:
                response["second_chance_seconds"] = max(
                    0,
                    math.ceil(self.second_chance_deadline - time.monotonic()),
                )
            if player_id in self.players:
                player = self.players[player_id]
                response.update(
                    {
                        "powerups": [dict(powerup) for powerup in PLAYER_POWERUPS],
                        "used_powerups": sorted(player["used_powerups"]),
                        "eliminated_answers": list(player["eliminated_answers"]),
                        "scouted_votes": (
                            list(player["scouted_votes"])
                            if player["scouted_votes"] is not None
                            else None
                        ),
                        "scout_available": any(
                            other_player_id != player_id
                            for other_player_id in self.answers
                            if not self.players[other_player_id][
                                "second_chance_wrong_answer"
                            ]
                        ),
                        "can_change_answer": player["can_change_answer"],
                        "second_chance_wrong_answer": player[
                            "second_chance_wrong_answer"
                        ],
                        "hide_answers": hide_answers,
                        "hide_answers_available": (
                            self.phase == "question"
                            and bool(self._eligible_hide_answer_targets(player_id))
                        ),
                    }
                )
            if player_id in self.players and player_id in self.answers:
                response["your_answer"] = self.answers[player_id]
            return response

    def add_player(self, name):
        with self.lock:
            if self.phase != "lobby":
                raise RequestError("This quiz has already started.", 409)
            if len(self.players) >= 100:
                raise RequestError("This room is full.", 409)
            player_id = secrets.token_urlsafe(18)
            reconnect_code = self._new_reconnect_code()
            self.players[player_id] = {
                "name": name,
                "score": 0,
                "reconnect_code": reconnect_code,
                "used_powerups": set(),
                "eliminated_answers": [],
                "scouted_votes": None,
                "can_change_answer": False,
                "second_chance_wrong_answer": False,
                "hide_answers": False,
            }
            return player_id

    def _new_reconnect_code(self):
        used_codes = {
            player["reconnect_code"] for player in self.players.values()
        }
        while True:
            code = "".join(
                secrets.choice(ROOM_ALPHABET)
                for _ in range(PLAYER_REJOIN_CODE_LENGTH)
            )
            if code not in used_codes:
                return code

    def _eligible_hide_answer_targets(self, player_id):
        return [
            other_player_id
            for other_player_id, player in self.players.items()
            if other_player_id != player_id
            and not player["hide_answers"]
            and (
                other_player_id not in self.answers
                or player["second_chance_wrong_answer"]
            )
        ]

    def rejoin_player(self, reconnect_code):
        if not isinstance(reconnect_code, str):
            raise RequestError("Reconnect code was not found.", 404)
        normalized_code = reconnect_code.strip().upper()
        with self.lock:
            for player_id, player in self.players.items():
                if secrets.compare_digest(
                    normalized_code, player["reconnect_code"]
                ):
                    return player_id
        raise RequestError("Reconnect code was not found in this room.", 404)

    def snapshot_data(self):
        with self.lock:
            players = [
                {
                    "player_id": player_id,
                    "name": player["name"],
                    "score": player["score"],
                    "reconnect_code": player["reconnect_code"],
                    "used_powerups": sorted(player["used_powerups"]),
                    "eliminated_answers": list(player["eliminated_answers"]),
                    "scouted_votes": (
                        list(player["scouted_votes"])
                        if player["scouted_votes"] is not None
                        else None
                    ),
                    "can_change_answer": player["can_change_answer"],
                    "second_chance_wrong_answer": player[
                        "second_chance_wrong_answer"
                    ],
                    "hide_answers": player["hide_answers"],
                }
                for player_id, player in self.players.items()
            ]
            leaderboard = [
                {
                    "name": player["name"],
                    "score": player["score"],
                    "reconnect_code": player["reconnect_code"],
                    "has_answered": player["player_id"] in self.answers,
                }
                for player in players
            ]
            leaderboard.sort(key=lambda player: (-player["score"], player["name"].casefold()))
            question_elapsed = None
            if self.phase == "question" and self.question_started_at is not None:
                question_elapsed = max(
                    0, time.monotonic() - self.question_started_at
                )
            second_chance_remaining = None
            if self.second_chance_deadline is not None:
                second_chance_remaining = max(
                    0, self.second_chance_deadline - time.monotonic()
                )
            return {
                "version": SNAPSHOT_VERSION,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "room_code": self.code,
                "host_token": self.host_token,
                "title": self.quiz["title"],
                "mode": self.mode,
                "phase": self.phase,
                "question_number": self.question_index + 1 if self.question_index >= 0 else None,
                "question_index": self.question_index,
                "question_elapsed_seconds": question_elapsed,
                "second_chance_seconds_remaining": second_chance_remaining,
                "modifier": self.modifier.key if self.modifier is not None else None,
                "quiz": self.quiz,
                "players": players,
                "leaderboard": leaderboard,
                "answers": dict(self.answers),
            }

    @classmethod
    def from_snapshot(cls, data):
        if not isinstance(data, dict):
            raise ValueError("Unsupported snapshot format.")
        snapshot_version = data.get("version")
        if (
            not isinstance(snapshot_version, int)
            or isinstance(snapshot_version, bool)
            or snapshot_version not in (1, SNAPSHOT_VERSION)
        ):
            raise ValueError("Unsupported snapshot format.")
        code = data.get("room_code")
        host_token = data.get("host_token")
        mode = data.get("mode")
        phase = data.get("phase")
        question_index = data.get("question_index")
        if not isinstance(code, str) or not code:
            raise ValueError("Snapshot has an invalid room code.")
        if not isinstance(host_token, str) or not host_token:
            raise ValueError("Snapshot has an invalid host token.")
        if mode not in ("manual", "auto"):
            raise ValueError("Snapshot has an invalid game mode.")
        if phase not in ("lobby", "question", "revealed", "completed"):
            raise ValueError("Snapshot has an invalid game phase.")
        if (
            not isinstance(question_index, int)
            or isinstance(question_index, bool)
            or question_index < -1
        ):
            raise ValueError("Snapshot has an invalid question index.")
        quiz = validate_quiz(data.get("quiz"))
        if question_index >= len(quiz["questions"]):
            raise ValueError("Snapshot question index is outside the quiz.")
        if (phase == "lobby") != (question_index == -1):
            raise ValueError("Snapshot phase and question index do not match.")

        modifier_key = data.get("modifier")
        modifier = None
        if modifier_key is not None:
            modifier = next(
                (item for item in QuestionModifier.ALL if item.key == modifier_key),
                None,
            )
            if modifier is None:
                raise ValueError("Snapshot has an unknown question modifier.")

        room = cls(code, quiz, host_token, mode)
        room.phase = phase
        room.question_index = question_index
        room.modifier = modifier
        player_records = data.get("players")
        if not isinstance(player_records, list) or len(player_records) > 100:
            raise ValueError("Snapshot has an invalid player list.")
        used_reconnect_codes = set()
        for record in player_records:
            if not isinstance(record, dict):
                raise ValueError("Snapshot has an invalid player record.")
            player_id = record.get("player_id")
            name = record.get("name")
            score = record.get("score")
            reconnect_code = record.get("reconnect_code")
            used_powerups = record.get("used_powerups")
            eliminated_answers = record.get("eliminated_answers")
            scouted_votes = record.get("scouted_votes")
            can_change_answer = record.get("can_change_answer")
            second_chance_wrong_answer = record.get(
                "second_chance_wrong_answer", False
            )
            hide_answers = (
                record.get("hide_answers", False)
                if snapshot_version >= SNAPSHOT_VERSION
                else False
            )
            if (
                not isinstance(player_id, str)
                or not player_id
                or player_id in room.players
                or not isinstance(name, str)
                or not name
                or not isinstance(score, int)
                or isinstance(score, bool)
                or not isinstance(reconnect_code, str)
                or len(reconnect_code) != PLAYER_REJOIN_CODE_LENGTH
                or any(character not in ROOM_ALPHABET for character in reconnect_code)
                or reconnect_code in used_reconnect_codes
                or not isinstance(used_powerups, list)
                or any(key not in PLAYER_POWERUP_KEYS for key in used_powerups)
                or not isinstance(eliminated_answers, list)
                or any(
                    not isinstance(index, int) or isinstance(index, bool)
                    for index in eliminated_answers
                )
                or (
                    scouted_votes is not None
                    and (
                        not isinstance(scouted_votes, list)
                        or any(
                            not isinstance(count, int)
                            or isinstance(count, bool)
                            or count < 0
                            for count in scouted_votes
                        )
                    )
                )
                or not isinstance(can_change_answer, bool)
                or not isinstance(second_chance_wrong_answer, bool)
                or not isinstance(hide_answers, bool)
            ):
                raise ValueError("Snapshot has an invalid player record.")
            used_reconnect_codes.add(reconnect_code)
            room.players[player_id] = {
                "name": name,
                "score": score,
                "reconnect_code": reconnect_code,
                "used_powerups": set(used_powerups),
                "eliminated_answers": list(eliminated_answers),
                "scouted_votes": (
                    list(scouted_votes) if scouted_votes is not None else None
                ),
                "can_change_answer": can_change_answer,
                "second_chance_wrong_answer": second_chance_wrong_answer,
                "hide_answers": hide_answers,
            }

        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise ValueError("Snapshot has invalid submitted answers.")
        for player_id, answer_index in answers.items():
            if (
                player_id not in room.players
                or not isinstance(answer_index, int)
                or isinstance(answer_index, bool)
                or question_index < 0
                or not 0
                <= answer_index
                < len(quiz["questions"][question_index]["answers"])
            ):
                raise ValueError("Snapshot has invalid submitted answers.")
        room.answers = answers

        elapsed = data.get("question_elapsed_seconds")
        if elapsed is None:
            elapsed = 0
        if (
            not isinstance(elapsed, (int, float))
            or isinstance(elapsed, bool)
            or not math.isfinite(elapsed)
            or elapsed < 0
        ):
            raise ValueError("Snapshot has invalid question timing.")
        remaining = data.get("second_chance_seconds_remaining")
        if remaining is not None and (
            not isinstance(remaining, (int, float))
            or isinstance(remaining, bool)
            or not math.isfinite(remaining)
            or remaining < 0
        ):
            raise ValueError("Snapshot has invalid second-chance timing.")
        if phase != "question" and remaining is not None:
            raise ValueError("Snapshot has invalid second-chance timing.")
        room._restore_timers(elapsed, remaining)
        return room

    def _restore_timers(self, elapsed, second_chance_remaining):
        if self.phase == "question":
            now = time.monotonic()
            self.question_started_at = now - elapsed
            if second_chance_remaining is not None:
                self.second_chance_deadline = now + second_chance_remaining
            time_remaining = (
                self.modifier.time_limit - elapsed
                if self.modifier is not None
                and self.modifier.time_limit is not None
                else None
            )
            if (
                second_chance_remaining is not None
                and second_chance_remaining <= 0
            ) or (
                time_remaining is not None
                and time_remaining <= 0
                and self.second_chance_deadline is None
            ):
                self.second_chance_deadline = None
                self._reveal_locked()
                return
            if time_remaining is not None and time_remaining > 0:
                timer = threading.Timer(
                    time_remaining,
                    self._question_timeout,
                    args=(self.question_index,),
                )
                timer.daemon = True
                timer.start()
            if second_chance_remaining is not None:
                timer = threading.Timer(
                    second_chance_remaining,
                    self._auto_reveal_after_second_chance,
                    args=(self.question_index,),
                )
                timer.daemon = True
                timer.start()
        elif self.phase == "revealed" and self.mode == "auto":
            timer = threading.Timer(
                AUTO_ADVANCE_SECONDS,
                self._auto_advance,
                args=(self.question_index,),
            )
            timer.daemon = True
            timer.start()

    def submit_answer(self, player_id, answer_index):
        with self.lock:
            if player_id not in self.players:
                raise RequestError("Player not found in this room.", 403)
            if self.phase != "question":
                raise RequestError("Answers are not being accepted right now.", 409)
            player = self.players[player_id]
            already_answered = player_id in self.answers
            if already_answered and not player["can_change_answer"]:
                raise RequestError("You have already answered this question.", 409)
            if (
                self.modifier is not None
                and self.modifier.time_limit is not None
                and time.monotonic() - self.question_started_at
                >= self.modifier.time_limit
                and not (
                    already_answered
                    and self.mode == "auto"
                    and self.second_chance_deadline is not None
                )
            ):
                self._reveal_locked()
                raise RequestError("Time is up for this question.", 409)
            if (
                self.second_chance_deadline is not None
                and time.monotonic() >= self.second_chance_deadline
            ):
                self.second_chance_deadline = None
                self._reveal_locked()
                raise RequestError("The second-chance window has closed.", 409)
            answers = self.quiz["questions"][self.question_index]["answers"]
            if not isinstance(answer_index, int) or isinstance(answer_index, bool):
                raise RequestError("Answer selection must be a number.")
            if not 0 <= answer_index < len(answers):
                raise RequestError("That answer does not exist.")
            if answer_index in player["eliminated_answers"]:
                raise RequestError("That answer was eliminated by your 50:50 power-up.", 409)
            if (
                already_answered
                and player["second_chance_wrong_answer"]
                and answer_index == self.answers[player_id]
            ):
                raise RequestError(
                    "Choose a different answer for your second guess.", 409
                )
            self.answers[player_id] = answer_index
            if already_answered:
                player["can_change_answer"] = False
                player["second_chance_wrong_answer"] = False
            elif player["can_change_answer"]:
                player["second_chance_wrong_answer"] = not answers[answer_index][
                    "correct"
                ]
                if not player["second_chance_wrong_answer"]:
                    player["can_change_answer"] = False
            if (
                self.mode == "auto"
                and self.players
                and len(self.answers) == len(self.players)
            ):
                self._begin_auto_reveal_locked()

    def activate_powerup(self, player_id, powerup_key):
        with self.lock:
            if player_id not in self.players:
                raise RequestError("Player not found in this room.", 403)
            if self.phase != "question":
                raise RequestError("Power-ups are only available during a question.", 409)
            if not isinstance(powerup_key, str) or powerup_key not in PLAYER_POWERUP_KEYS:
                raise RequestError("That power-up does not exist.")

            if (
                self.second_chance_deadline is not None
                and time.monotonic() >= self.second_chance_deadline
            ):
                self.second_chance_deadline = None
                self._reveal_locked()
                raise RequestError("The second-chance window has closed.", 409)

            player = self.players[player_id]
            if powerup_key in player["used_powerups"]:
                raise RequestError("You have already used this power-up.", 409)
            already_answered = player_id in self.answers
            is_second_chance = powerup_key == "second_chance"
            if already_answered:
                raise RequestError(
                    "Use this power-up before submitting your answer.", 409
                )

            if (
                self.modifier is not None
                and self.modifier.time_limit is not None
                and time.monotonic() - self.question_started_at
                >= self.modifier.time_limit
            ):
                self._reveal_locked()
                raise RequestError("Time is up for this question.", 409)

            if powerup_key == "fifty_fifty":
                answer_count = len(
                    self.quiz["questions"][self.question_index]["answers"]
                )
                if answer_count < 3:
                    raise RequestError(
                        "50:50 requires at least three answer choices.", 409
                    )
                answers = self.quiz["questions"][self.question_index]["answers"]
                correct_index = next(
                    index for index, answer in enumerate(answers) if answer["correct"]
                )
                wrong_indices = [
                    index for index in range(answer_count) if index != correct_index
                ]
                player["eliminated_answers"] = random.sample(
                    wrong_indices,
                    answer_count // 2,
                )
            elif powerup_key == "scout":
                other_answers = [
                    answer_index
                    for other_player_id, answer_index in self.answers.items()
                    if other_player_id != player_id
                    and not self.players[other_player_id][
                        "second_chance_wrong_answer"
                    ]
                ]
                if not other_answers:
                    raise RequestError(
                        "Scout is available after another player has answered.", 409
                    )
                answer_count = len(
                    self.quiz["questions"][self.question_index]["answers"]
                )
                votes = [0] * answer_count
                for answer_index in other_answers:
                    votes[answer_index] += 1
                player["scouted_votes"] = votes
            elif powerup_key == "hide_answers":
                eligible_targets = self._eligible_hide_answer_targets(player_id)
                if not eligible_targets:
                    raise RequestError(
                        "Memory test requires another player who has not locked an answer.",
                        409,
                    )
                for target_id in eligible_targets:
                    self.players[target_id]["hide_answers"] = True
            elif is_second_chance:
                player["can_change_answer"] = True

            player["used_powerups"].add(powerup_key)

    def host_action(self, host_token, action):
        with self.lock:
            if not secrets.compare_digest(host_token, self.host_token):
                raise RequestError("Invalid host token.", 403)

            if action == "start" and self.phase == "lobby":
                if self.mode == "auto" and not self.players:
                    raise RequestError(
                        "At least one player must join before starting auto mode.",
                        409,
                    )
                self.question_index = 0
                self.answers = {}
                self._start_question_locked()
                return

            if action == "reveal" and self.phase == "question":
                self._reveal_locked()
                return

            if action == "next" and self.phase == "revealed":
                if self.mode == "auto":
                    raise RequestError("Questions advance automatically in this mode.", 409)
                self._advance_locked()
                return

            raise RequestError("That action is not available right now.", 409)

    def _reveal_locked(self):
        current_answers = self.quiz["questions"][self.question_index]["answers"]
        for player_id, answer_index in self.answers.items():
            if current_answers[answer_index]["correct"]:
                points = self.modifier.correct_points if self.modifier else 1
                self.players[player_id]["score"] += points
            elif self.modifier is not None:
                self.players[player_id]["score"] -= self.modifier.wrong_penalty
        self.phase = "revealed"
        self.question_started_at = None
        self.second_chance_deadline = None
        for player in self.players.values():
            player["can_change_answer"] = False
            player["second_chance_wrong_answer"] = False
        if self.mode == "auto":
            timer = threading.Timer(
                AUTO_ADVANCE_SECONDS,
                self._auto_advance,
                args=(self.question_index,),
            )
            timer.daemon = True
            timer.start()

    def _start_question_locked(self):
        self.phase = "question"
        self.second_chance_deadline = None
        for player in self.players.values():
            player["eliminated_answers"] = []
            player["scouted_votes"] = None
            player["can_change_answer"] = False
            player["second_chance_wrong_answer"] = False
            player["hide_answers"] = False
        self.modifier = QuestionModifier.for_question(self.question_index + 1)
        self.question_started_at = time.monotonic()
        if self.modifier is not None and self.modifier.time_limit is not None:
            timer = threading.Timer(
                self.modifier.time_limit,
                self._question_timeout,
                args=(self.question_index,),
            )
            timer.daemon = True
            timer.start()

    def _question_timeout(self, question_index):
        with self.lock:
            if (
                self.phase == "question"
                and self.question_index == question_index
                and self.second_chance_deadline is None
                and self.modifier is not None
                and self.modifier.time_limit is not None
            ):
                self._reveal_locked()

    def _begin_auto_reveal_locked(self):
        if any(
            player["second_chance_wrong_answer"]
            for player in self.players.values()
        ):
            if self.second_chance_deadline is None:
                self.second_chance_deadline = (
                    time.monotonic() + SECOND_CHANCE_WINDOW_SECONDS
                )
                timer = threading.Timer(
                    SECOND_CHANCE_WINDOW_SECONDS,
                    self._auto_reveal_after_second_chance,
                    args=(self.question_index,),
                )
                timer.daemon = True
                timer.start()
            return
        self.second_chance_deadline = None
        self._reveal_locked()

    def _auto_reveal_after_second_chance(self, question_index):
        with self.lock:
            if (
                self.phase != "question"
                or self.question_index != question_index
                or self.second_chance_deadline is None
            ):
                return
            remaining = self.second_chance_deadline - time.monotonic()
            if remaining > 0:
                timer = threading.Timer(
                    remaining,
                    self._auto_reveal_after_second_chance,
                    args=(question_index,),
                )
                timer.daemon = True
                timer.start()
                return
            self.second_chance_deadline = None
            self._reveal_locked()

    def _auto_advance(self, question_index):
        with self.lock:
            if (
                self.mode == "auto"
                and self.phase == "revealed"
                and self.question_index == question_index
            ):
                self._advance_locked()

    def _advance_locked(self):
        if self.question_index + 1 >= len(self.quiz["questions"]):
            self.phase = "completed"
            return
        self.question_index += 1
        self.answers = {}
        self._start_question_locked()


class SnapshotStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.Lock()

    def save_room(self, room):
        with self.lock:
            snapshot = room.snapshot_data()
            content = json.dumps(snapshot, ensure_ascii=False, indent=2)
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / "{}.json".format(room.code)
            temporary_path = self.directory / "{}.{}.tmp".format(
                room.code, secrets.token_hex(4)
            )
            temporary_path.write_text(content, encoding="utf-8")
            os.replace(str(temporary_path), str(path))

    def load_rooms(self):
        if not self.directory.exists():
            return []
        restored = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                room = QuizRoom.from_snapshot(data)
                if path.stem.upper() != room.code.upper():
                    raise ValueError("Snapshot filename does not match its room code.")
            except (OSError, ValueError, TypeError, KeyError, RequestError) as error:
                print(
                    "Could not restore room snapshot {}: {}".format(path, error),
                    file=sys.stderr,
                )
                continue
            restored.append(room)
        return restored


SNAPSHOT_STORE = SnapshotStore(SNAPSHOT_DIR)


def persist_room(room):
    try:
        SNAPSHOT_STORE.save_room(room)
    except OSError as error:
        print(
            "Could not save snapshot for room {}: {}".format(room.code, error),
            file=sys.stderr,
        )


def persist_all_rooms():
    with ROOMS_LOCK:
        rooms = list(ROOMS.values())
    for room in rooms:
        persist_room(room)


def restore_saved_rooms():
    restored = SNAPSHOT_STORE.load_rooms()
    with ROOMS_LOCK:
        for room in restored:
            if room.code in ROOMS:
                print(
                    "Could not restore room {}: room code is already in use.".format(
                        room.code
                    ),
                    file=sys.stderr,
                )
                continue
            ROOMS[room.code] = room
    print("Restored {} saved quiz room(s).".format(len(restored)))


def create_room(
    quiz_data=None,
    mode="manual",
    csv_content=None,
    quiz_title=None,
    randomize_questions=False,
):
    if mode not in ("manual", "auto"):
        raise RequestError("Game mode must be 'manual' or 'auto'.")
    if not isinstance(randomize_questions, bool):
        raise RequestError("randomize_questions must be a boolean.")
    if csv_content is None:
        quiz = validate_quiz(quiz_data)
        import_warnings = []
    else:
        quiz, import_warnings = parse_csv_quiz(csv_content, quiz_title)
    for question in quiz["questions"]:
        random.shuffle(question["answers"])
    if randomize_questions:
        random.shuffle(quiz["questions"])
    with ROOMS_LOCK:
        for _ in range(10):
            code = "".join(secrets.choice(ROOM_ALPHABET) for _ in range(5))
            if code not in ROOMS:
                break
        else:
            raise RequestError("Could not create a room. Please try again.", 503)
        host_token = secrets.token_urlsafe(32)
        ROOMS[code] = QuizRoom(code, quiz, host_token, mode)
    return code, host_token, import_warnings


def get_room(code):
    with ROOMS_LOCK:
        room = ROOMS.get(code.upper())
    if room is None:
        raise RequestError("Room not found. Check the room code.", 404)
    return room


class QuizRequestHandler(BaseHTTPRequestHandler):
    server_version = "LocalQuiz/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            params = parse_qs(parsed.query)
            code = params.get("room", [""])[0]
            player_id = params.get("player", [None])[0]
            try:
                room = get_room(code)
                host_token = self.headers.get("X-Host-Token", "")
                include_reconnect_codes = (
                    player_id is None
                    and bool(host_token)
                    and secrets.compare_digest(host_token, room.host_token)
                )
                self.send_json(
                    200,
                    room.public_state(
                        player_id,
                        include_reconnect_codes=include_reconnect_codes,
                    ),
                )
            except RequestError as error:
                self.send_error_json(error)
            return

        static_files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
        }
        asset = static_files.get(parsed.path)
        if asset is None:
            self.send_error_json(RequestError("Not found.", 404))
            return
        file_path = STATIC_DIR / asset[0]
        try:
            content = file_path.read_bytes()
        except OSError:
            self.send_error_json(RequestError("App files are unavailable.", 500))
            return
        self.send_response(200)
        self.send_header("Content-Type", asset[1])
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        try:
            data = self.read_json()
            if self.path == "/api/rooms":
                if "quiz_csv" in data:
                    code, host_token, import_warnings = create_room(
                        mode=data.get("mode", "manual"),
                        csv_content=data.get("quiz_csv"),
                        quiz_title=data.get("quiz_title"),
                        randomize_questions=data.get("randomize_questions", False),
                    )
                else:
                    code, host_token, import_warnings = create_room(
                        data.get("quiz"),
                        data.get("mode", "manual"),
                        randomize_questions=data.get("randomize_questions", False),
                    )
                persist_room(get_room(code))
                self.send_json(
                    201,
                    {
                        "room_code": code,
                        "host_token": host_token,
                        "import_warnings": import_warnings,
                    },
                )
            elif self.path == "/api/join":
                code = self.required_string(data, "room_code", 5)
                name = self.required_string(data, "name", 30)
                room = get_room(code)
                player_id = room.add_player(name)
                persist_room(room)
                self.send_json(
                    201,
                    {
                        "room_code": code.upper(),
                        "player_id": player_id,
                        "reconnect_code": room.players[player_id]["reconnect_code"],
                    },
                )
            elif self.path == "/api/rejoin":
                code = self.required_string(data, "room_code", 5)
                reconnect_code = self.required_string(
                    data, "reconnect_code", PLAYER_REJOIN_CODE_LENGTH
                )
                room = get_room(code)
                player_id = room.rejoin_player(reconnect_code)
                self.send_json(
                    200,
                    {
                        "room_code": code.upper(),
                        "player_id": player_id,
                        "reconnect_code": room.players[player_id]["reconnect_code"],
                    },
                )
            elif self.path == "/api/answer":
                code = self.required_string(data, "room_code", 5)
                player_id = self.required_string(data, "player_id", 100)
                room = get_room(code)
                room.submit_answer(player_id, data.get("answer_index"))
                persist_room(room)
                self.send_json(200, {"ok": True})
            elif self.path == "/api/powerup":
                code = self.required_string(data, "room_code", 5)
                player_id = self.required_string(data, "player_id", 100)
                room = get_room(code)
                room.activate_powerup(player_id, data.get("powerup"))
                persist_room(room)
                self.send_json(200, {"ok": True})
            elif self.path == "/api/host/action":
                code = self.required_string(data, "room_code", 5)
                host_token = self.required_string(data, "host_token", 100)
                action = self.required_string(data, "action", 20)
                room = get_room(code)
                room.host_action(host_token, action)
                persist_room(room)
                self.send_json(200, {"ok": True})
            else:
                self.send_error_json(RequestError("Not found.", 404))
        except RequestError as error:
            self.send_error_json(error)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_error_json(RequestError("Request body must be valid JSON."))

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise RequestError("Invalid content length.")
        if length < 0 or length > MAX_BODY_SIZE:
            raise RequestError("Request body is too large.", 413)
        raw_body = self.rfile.read(length)
        data = json.loads(raw_body.decode("utf-8"))
        if not isinstance(data, dict):
            raise RequestError("Request body must be a JSON object.")
        return data

    @staticmethod
    def required_string(data, field, max_length):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
            raise RequestError("{} must be between 1 and {} characters.".format(field, max_length))
        return value.strip()

    def send_json(self, status, data):
        content = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def send_error_json(self, error):
        self.send_json(error.status, {"error": str(error)})

    def log_message(self, format_string, *args):
        print("{} - {}".format(self.address_string(), format_string % args))


def lan_addresses():
    addresses = set()
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = result[4][0]
            if not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    return sorted(addresses)


def snapshot_loop(stop_event):
    while not stop_event.wait(ROOM_SNAPSHOT_INTERVAL_SECONDS):
        persist_all_rooms()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("0.0.0.0", port), QuizRequestHandler)
    restore_saved_rooms()
    snapshot_stop = threading.Event()
    snapshot_thread = threading.Thread(
        target=snapshot_loop,
        args=(snapshot_stop,),
        daemon=True,
    )
    snapshot_thread.start()
    print("Quiz server running:")
    print("  Host: http://localhost:{}".format(port))
    for address in lan_addresses():
        print("  LAN:  http://{}:{}".format(address, port))
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping quiz server.")
    finally:
        snapshot_stop.set()
        snapshot_thread.join()
        persist_all_rooms()
        server.server_close()


if __name__ == "__main__":
    main()

import json
import secrets
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
MAX_BODY_SIZE = 1_000_000
AUTO_ADVANCE_SECONDS = 3
ROOM_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
ROOMS = {}
ROOMS_LOCK = threading.RLock()


class RequestError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


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
        answers = question.get("answers")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.strip()) > 500:
            raise RequestError(
                "Question {} must have text between 1 and 500 characters.".format(
                    question_number
                )
            )
        if not isinstance(answers, list) or not 2 <= len(answers) <= 6:
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
            if not isinstance(text, str) or not text.strip() or len(text.strip()) > 200:
                raise RequestError(
                    "Every answer in question {} must have text between 1 and 200 characters.".format(
                        question_number
                    )
                )
            if not isinstance(correct, bool):
                raise RequestError(
                    "The correct field in question {} must be true or false.".format(
                        question_number
                    )
                )
            correct_count += int(correct)
            validated_answers.append({"text": text.strip(), "correct": correct})

        if correct_count != 1:
            raise RequestError(
                "Question {} must have exactly one answer marked correct.".format(
                    question_number
                )
            )
        validated_questions.append(
            {"question": prompt.strip(), "answers": validated_answers}
        )

    return {"title": title.strip(), "questions": validated_questions}


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
        self.lock = threading.RLock()

    def public_state(self, player_id=None):
        with self.lock:
            question = None
            correct_answer = None
            correct_answer_index = None
            if self.phase in ("question", "revealed"):
                current = self.quiz["questions"][self.question_index]
                question = {
                    "number": self.question_index + 1,
                    "total": len(self.quiz["questions"]),
                    "text": current["question"],
                    "answers": [answer["text"] for answer in current["answers"]],
                }
                if self.phase == "revealed":
                    correct_answer_index = next(
                        index
                        for index, answer in enumerate(current["answers"])
                        if answer["correct"]
                    )
                    correct_answer = current["answers"][correct_answer_index]["text"]

            players = [
                {
                    "name": player["name"],
                    "score": player["score"],
                    "has_answered": player_id in self.answers,
                }
                for player_id, player in self.players.items()
            ]
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
            self.players[player_id] = {"name": name, "score": 0}
            return player_id

    def submit_answer(self, player_id, answer_index):
        with self.lock:
            if player_id not in self.players:
                raise RequestError("Player not found in this room.", 403)
            if self.phase != "question":
                raise RequestError("Answers are not being accepted right now.", 409)
            if player_id in self.answers:
                raise RequestError("You have already answered this question.", 409)
            answers = self.quiz["questions"][self.question_index]["answers"]
            if not isinstance(answer_index, int) or isinstance(answer_index, bool):
                raise RequestError("Answer selection must be a number.")
            if not 0 <= answer_index < len(answers):
                raise RequestError("That answer does not exist.")
            self.answers[player_id] = answer_index
            if (
                self.mode == "auto"
                and self.players
                and len(self.answers) == len(self.players)
            ):
                self._reveal_locked()

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
                self.phase = "question"
                self.question_index = 0
                self.answers = {}
                return

            if action == "reveal" and self.phase == "question":
                if self.mode == "auto":
                    raise RequestError("Answers are revealed automatically in this mode.", 409)
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
                self.players[player_id]["score"] += 1
        self.phase = "revealed"
        if self.mode == "auto":
            timer = threading.Timer(
                AUTO_ADVANCE_SECONDS,
                self._auto_advance,
                args=(self.question_index,),
            )
            timer.daemon = True
            timer.start()

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
        self.phase = "question"


def create_room(quiz_data, mode="manual"):
    quiz = validate_quiz(quiz_data)
    if mode not in ("manual", "auto"):
        raise RequestError("Game mode must be 'manual' or 'auto'.")
    with ROOMS_LOCK:
        for _ in range(10):
            code = "".join(secrets.choice(ROOM_ALPHABET) for _ in range(5))
            if code not in ROOMS:
                break
        else:
            raise RequestError("Could not create a room. Please try again.", 503)
        host_token = secrets.token_urlsafe(32)
        ROOMS[code] = QuizRoom(code, quiz, host_token, mode)
    return code, host_token


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
                self.send_json(200, get_room(code).public_state(player_id))
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
                code, host_token = create_room(data.get("quiz"), data.get("mode", "manual"))
                self.send_json(
                    201, {"room_code": code, "host_token": host_token}
                )
            elif self.path == "/api/join":
                code = self.required_string(data, "room_code", 5)
                name = self.required_string(data, "name", 30)
                player_id = get_room(code).add_player(name)
                self.send_json(201, {"room_code": code.upper(), "player_id": player_id})
            elif self.path == "/api/answer":
                code = self.required_string(data, "room_code", 5)
                player_id = self.required_string(data, "player_id", 100)
                get_room(code).submit_answer(player_id, data.get("answer_index"))
                self.send_json(200, {"ok": True})
            elif self.path == "/api/host/action":
                code = self.required_string(data, "room_code", 5)
                host_token = self.required_string(data, "host_token", 100)
                action = self.required_string(data, "action", 20)
                get_room(code).host_action(host_token, action)
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


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("0.0.0.0", port), QuizRequestHandler)
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
        server.server_close()


if __name__ == "__main__":
    main()

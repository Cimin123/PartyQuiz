import json
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch

from app import (
    AUTO_ADVANCE_SECONDS,
    QuizRequestHandler,
    QuizRoom,
    RequestError,
    parse_csv_quiz,
    validate_quiz,
)


VALID_QUIZ = {
    "title": "Test Quiz",
    "questions": [
        {
            "question": "Pick the right answer",
            "answers": [
                {"text": "Wrong"},
                {"text": "Right", "correct": True},
            ],
        }
    ],
}

SAMPLE_CSV = (
    "\ufeffKategoria,Pytanie,A,B,C,D,E,Poprawna odp,Include\n"
    'Mix,"Question, with a comma",Alpha,,Charlie,,,Alpha,1\n'
    "Mix,Missing correct,A,B,,,,,1\n"
    "Mix,Excluded,A,B,,,,A,0\n"
)


class QuizValidationTests(unittest.TestCase):
    def test_valid_quiz_is_normalized(self):
        quiz = validate_quiz(VALID_QUIZ)
        self.assertEqual(quiz["title"], "Test Quiz")
        self.assertEqual(quiz["questions"][0]["answers"][1]["text"], "Right")

    def test_question_requires_exactly_one_correct_answer(self):
        quiz = {
            "title": "Bad Quiz",
            "questions": [
                {
                    "question": "Ambiguous?",
                    "answers": [{"text": "A"}, {"text": "B"}],
                }
            ],
        }
        with self.assertRaisesRegex(RequestError, "exactly one"):
            validate_quiz(quiz)

    def test_csv_import_fills_blank_choices_and_reports_skipped_rows(self):
        quiz, warnings = parse_csv_quiz(SAMPLE_CSV, "CSV Test")

        self.assertEqual(len(quiz["questions"]), 1)
        self.assertEqual(quiz["questions"][0]["category"], "Mix")
        self.assertEqual(
            [answer["text"] for answer in quiz["questions"][0]["answers"]],
            ["Alpha", "x", "Charlie", "x", "x"],
        )
        self.assertEqual(
            [answer["correct"] for answer in quiz["questions"][0]["answers"]],
            [True, False, False, False, False],
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("row 3 skipped: the correct answer is blank", warnings[0])

    def test_csv_import_fails_when_no_rows_have_a_correct_answer(self):
        csv_content = "Pytanie,A,B,Poprawna odp\nQuestion,A,B,\n"
        with self.assertRaisesRegex(RequestError, "no valid questions"):
            parse_csv_quiz(csv_content, "CSV Test")


class QuizRoomTests(unittest.TestCase):
    def setUp(self):
        self.room = QuizRoom("ABCDE", validate_quiz(VALID_QUIZ), "host-secret")
        self.player_id = self.room.add_player("Player")

    def test_correct_answer_is_hidden_until_host_reveals(self):
        self.room.host_action("host-secret", "start")
        state = self.room.public_state(self.player_id)
        self.assertIsNone(state["correct_answer"])
        self.room.submit_answer(self.player_id, 1)
        self.room.host_action("host-secret", "reveal")
        state = self.room.public_state(self.player_id)
        self.assertEqual(state["correct_answer"], "Right")
        self.assertEqual(self.room.players[self.player_id]["score"], 1)

    def test_player_can_only_answer_once_per_question(self):
        self.room.host_action("host-secret", "start")
        self.room.submit_answer(self.player_id, 0)
        with self.assertRaisesRegex(RequestError, "already answered"):
            self.room.submit_answer(self.player_id, 1)

    def test_host_token_is_required_for_game_control(self):
        with self.assertRaisesRegex(RequestError, "Invalid host token"):
            self.room.host_action("not-the-host", "start")

    def test_auto_mode_reveals_after_every_player_answers_then_advances(self):
        room = QuizRoom("ABCDE", validate_quiz(VALID_QUIZ), "host-secret", "auto")
        first_player = room.add_player("First")
        second_player = room.add_player("Second")
        timers = []

        class CapturedTimer:
            def __init__(self, interval, function, args=(), kwargs=None):
                self.interval = interval
                self.function = function
                self.args = args
                timers.append(self)

            def start(self):
                pass

        with patch("app.threading.Timer", CapturedTimer):
            room.host_action("host-secret", "start")
            room.submit_answer(first_player, 1)
            self.assertEqual(room.public_state()["phase"], "question")

            room.submit_answer(second_player, 0)
            state = room.public_state(first_player)

            self.assertEqual(state["phase"], "revealed")
            self.assertEqual(state["correct_answer"], "Right")
            self.assertEqual(room.players[first_player]["score"], 1)
            self.assertEqual(len(timers), 1)
            self.assertEqual(timers[0].interval, AUTO_ADVANCE_SECONDS)

            timers[0].function(*timers[0].args)
            self.assertEqual(room.public_state()["phase"], "completed")

    def test_auto_mode_requires_players_and_disables_manual_controls(self):
        room = QuizRoom("ABCDE", validate_quiz(VALID_QUIZ), "host-secret", "auto")
        with self.assertRaisesRegex(RequestError, "At least one player"):
            room.host_action("host-secret", "start")

        room.add_player("Player")
        room.host_action("host-secret", "start")
        with self.assertRaisesRegex(RequestError, "revealed automatically"):
            room.host_action("host-secret", "reveal")


class QuizApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), QuizRequestHandler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = "http://127.0.0.1:{}".format(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_room_creation_join_and_synchronized_question_flow(self):
        status, room = self.request("/api/rooms", {"quiz": VALID_QUIZ})
        self.assertEqual(status, 201)
        code = room["room_code"]

        status, player = self.request(
            "/api/join", {"room_code": code, "name": "Player"}
        )
        self.assertEqual(status, 201)

        status, state = self.request("/api/state?room={}".format(code))
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "lobby")
        self.assertIsNone(state["question"])

        self.request(
            "/api/host/action",
            {"room_code": code, "host_token": room["host_token"], "action": "start"},
        )
        status, state = self.request(
            "/api/state?room={}&player={}".format(code, player["player_id"])
        )
        self.assertEqual(status, 200)
        self.assertEqual(state["phase"], "question")
        self.assertIsNone(state["correct_answer"])
        self.assertIsNone(state["correct_answer_index"])

        self.request(
            "/api/answer",
            {
                "room_code": code,
                "player_id": player["player_id"],
                "answer_index": 1,
            },
        )
        self.request(
            "/api/host/action",
            {"room_code": code, "host_token": room["host_token"], "action": "reveal"},
        )
        status, state = self.request("/api/state?room={}".format(code))
        self.assertEqual(status, 200)
        self.assertEqual(state["correct_answer"], "Right")
        self.assertEqual(state["players"][0]["score"], 1)

    def test_room_creation_accepts_automatic_mode(self):
        status, room = self.request(
            "/api/rooms", {"quiz": VALID_QUIZ, "mode": "auto"}
        )
        self.assertEqual(status, 201)
        status, state = self.request("/api/state?room={}".format(room["room_code"]))
        self.assertEqual(status, 200)
        self.assertEqual(state["mode"], "auto")

    def test_room_creation_accepts_csv_and_returns_import_warnings(self):
        status, room = self.request(
            "/api/rooms",
            {"quiz_csv": SAMPLE_CSV, "quiz_title": "CSV Test", "mode": "manual"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(len(room["import_warnings"]), 1)
        status, state = self.request("/api/state?room={}".format(room["room_code"]))
        self.assertEqual(status, 200)
        self.assertEqual(state["title"], "CSV Test")
        self.assertEqual(state["phase"], "lobby")


if __name__ == "__main__":
    unittest.main()

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
    QuestionModifier,
    RequestError,
    SECOND_CHANCE_WINDOW_SECONDS,
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

    def test_fifty_fifty_eliminates_answers_for_only_the_player_who_used_it(self):
        quiz = validate_quiz(
            {
                "title": "Four choices",
                "questions": [
                    {
                        "question": "Pick one",
                        "answers": [
                            {"text": "A"},
                            {"text": "B"},
                            {"text": "C", "correct": True},
                            {"text": "D"},
                        ],
                    }
                ],
            }
        )
        room = QuizRoom("FIFTY", quiz, "host-secret")
        first_player = room.add_player("First")
        second_player = room.add_player("Second")
        room.host_action("host-secret", "start")

        with patch("app.random.sample", return_value=[0, 1]):
            room.activate_powerup(first_player, "fifty_fifty")

        self.assertEqual(
            room.public_state(first_player)["eliminated_answers"], [0, 1]
        )
        self.assertNotIn("eliminated_answers", room.public_state())
        self.assertEqual(room.public_state(second_player)["eliminated_answers"], [])
        with self.assertRaisesRegex(RequestError, "eliminated"):
            room.submit_answer(first_player, 0)
        room.submit_answer(first_player, 2)
        with self.assertRaisesRegex(RequestError, "already used"):
            room.activate_powerup(first_player, "fifty_fifty")

    def test_fifty_fifty_does_not_spend_powerup_on_two_choice_questions(self):
        self.room.host_action("host-secret", "start")
        with self.assertRaisesRegex(RequestError, "at least three"):
            self.room.activate_powerup(self.player_id, "fifty_fifty")
        self.assertNotIn(
            "fifty_fifty",
            self.room.public_state(self.player_id)["used_powerups"],
        )

    def test_scout_shows_anonymous_votes_from_other_players_only(self):
        room = QuizRoom("SCOUT", validate_quiz(VALID_QUIZ), "host-secret")
        first_player = room.add_player("First")
        second_player = room.add_player("Second")
        scout_player = room.add_player("Scout")
        room.host_action("host-secret", "start")
        room.submit_answer(first_player, 0)
        room.submit_answer(second_player, 1)

        room.activate_powerup(scout_player, "scout")

        self.assertEqual(
            room.public_state(scout_player)["scouted_votes"], [1, 1]
        )
        self.assertIsNone(room.public_state(first_player)["scouted_votes"])
        self.assertEqual(
            room.players[scout_player]["used_powerups"], {"scout"}
        )

    def test_second_chance_allows_exactly_one_answer_change(self):
        self.room.host_action("host-secret", "start")
        self.room.submit_answer(self.player_id, 0)

        self.room.activate_powerup(self.player_id, "second_chance")
        self.assertTrue(
            self.room.public_state(self.player_id)["can_change_answer"]
        )
        self.room.submit_answer(self.player_id, 1)
        self.assertEqual(
            self.room.public_state(self.player_id)["your_answer"], 1
        )
        self.assertFalse(
            self.room.public_state(self.player_id)["can_change_answer"]
        )
        with self.assertRaisesRegex(RequestError, "already answered"):
            self.room.submit_answer(self.player_id, 0)
        self.room.host_action("host-secret", "reveal")
        self.assertEqual(self.room.players[self.player_id]["score"], 1)

    def test_modifier_is_selected_only_for_every_fifth_question(self):
        modifier = QuestionModifier.ALL[0]
        with patch("app.random.choice", return_value=modifier) as choose:
            self.assertIsNone(QuestionModifier.for_question(4))
            self.assertIs(QuestionModifier.for_question(5), modifier)
            self.assertEqual(choose.call_count, 1)

    def test_captain_modifier_is_sent_with_the_fifth_question(self):
        quiz = validate_quiz(
            {
                "title": "Five questions",
                "questions": VALID_QUIZ["questions"] * 5,
            }
        )
        room = QuizRoom("CAPTN", quiz, "host-secret")
        captain = next(
            modifier
            for modifier in QuestionModifier.ALL
            if modifier.key == "captain"
        )
        with patch("app.random.choice", return_value=captain):
            room.host_action("host-secret", "start")
            for _ in range(4):
                room.host_action("host-secret", "reveal")
                room.host_action("host-secret", "next")

        question = room.public_state()["question"]
        self.assertEqual(question["number"], 5)
        self.assertEqual(question["modifier"], captain.public_data())

    def test_opposing_team_compliment_modifier_is_sent_with_the_fifth_question(self):
        quiz = validate_quiz(
            {
                "title": "Five questions",
                "questions": VALID_QUIZ["questions"] * 5,
            }
        )
        room = QuizRoom("COMPL", quiz, "host-secret")
        compliment = next(
            modifier
            for modifier in QuestionModifier.ALL
            if modifier.key == "opposing_team_compliment"
        )
        with patch("app.random.choice", return_value=compliment):
            room.host_action("host-secret", "start")
            for _ in range(4):
                room.host_action("host-secret", "reveal")
                room.host_action("host-secret", "next")

        question = room.public_state()["question"]
        self.assertEqual(question["number"], 5)
        self.assertEqual(question["modifier"], compliment.public_data())

    def test_paulina_consultation_modifier_is_sent_with_the_fifth_question(self):
        quiz = validate_quiz(
            {
                "title": "Five questions",
                "questions": VALID_QUIZ["questions"] * 5,
            }
        )
        room = QuizRoom("PAULN", quiz, "host-secret")
        consultation = next(
            modifier
            for modifier in QuestionModifier.ALL
            if modifier.key == "consult_paulina"
        )
        with patch("app.random.choice", return_value=consultation):
            room.host_action("host-secret", "start")
            for _ in range(4):
                room.host_action("host-secret", "reveal")
                room.host_action("host-secret", "next")

        question = room.public_state()["question"]
        self.assertEqual(question["number"], 5)
        self.assertEqual(question["modifier"], consultation.public_data())

    def test_team_treat_modifier_is_sent_with_the_fifth_question(self):
        quiz = validate_quiz(
            {
                "title": "Five questions",
                "questions": VALID_QUIZ["questions"] * 5,
            }
        )
        room = QuizRoom("TREAT", quiz, "host-secret")
        team_treat = next(
            modifier
            for modifier in QuestionModifier.ALL
            if modifier.key == "team_treat"
        )
        with patch("app.random.choice", return_value=team_treat):
            room.host_action("host-secret", "start")
            for _ in range(4):
                room.host_action("host-secret", "reveal")
                room.host_action("host-secret", "next")

        question = room.public_state()["question"]
        self.assertEqual(question["number"], 5)
        self.assertEqual(question["modifier"], team_treat.public_data())

    def test_double_points_and_wrong_answer_penalty_are_scored(self):
        quiz = validate_quiz(
            {
                "title": "Five questions",
                "questions": VALID_QUIZ["questions"] * 5,
            }
        )
        double_room = QuizRoom("DOUBLE", quiz, "host-secret")
        player_id = double_room.add_player("Player")
        with patch(
            "app.random.choice",
            return_value=next(
                modifier
                for modifier in QuestionModifier.ALL
                if modifier.key == "double_points"
            ),
        ):
            double_room.host_action("host-secret", "start")
            for question_number in range(1, 6):
                double_room.submit_answer(player_id, 1)
                double_room.host_action("host-secret", "reveal")
                if question_number < 5:
                    double_room.host_action("host-secret", "next")
            self.assertEqual(double_room.players[player_id]["score"], 7)

        penalty_room = QuizRoom("PENAL", quiz, "host-secret")
        penalty_player = penalty_room.add_player("Player")
        with patch(
            "app.random.choice",
            return_value=next(
                modifier
                for modifier in QuestionModifier.ALL
                if modifier.key == "wrong_answer_penalty"
            ),
        ):
            penalty_room.host_action("host-secret", "start")
            for question_number in range(1, 5):
                penalty_room.host_action("host-secret", "reveal")
                penalty_room.host_action("host-secret", "next")
            penalty_room.submit_answer(penalty_player, 0)
            penalty_room.host_action("host-secret", "reveal")
            self.assertEqual(penalty_room.players[penalty_player]["score"], -2)

    def test_speed_round_timeout_reveals_the_question(self):
        quiz = validate_quiz(
            {
                "title": "Five questions",
                "questions": VALID_QUIZ["questions"] * 5,
            }
        )
        room = QuizRoom("SPEED", quiz, "host-secret")
        room.add_player("Player")
        timers = []

        class CapturedTimer:
            def __init__(self, interval, function, args=(), kwargs=None):
                self.interval = interval
                self.function = function
                self.args = args
                timers.append(self)

            def start(self):
                pass

        speed_round = next(
            modifier
            for modifier in QuestionModifier.ALL
            if modifier.key == "speed_round"
        )
        with patch("app.random.choice", return_value=speed_round), patch(
            "app.threading.Timer", CapturedTimer
        ):
            room.host_action("host-secret", "start")
            for _ in range(4):
                room.host_action("host-secret", "reveal")
                room.host_action("host-secret", "next")

            state = room.public_state()
            self.assertEqual(state["question"]["modifier"]["key"], "speed_round")
            self.assertEqual(state["question"]["time_remaining"], 15)
            self.assertEqual(timers[0].interval, 15)
            timers[0].function(*timers[0].args)
            self.assertEqual(room.public_state()["phase"], "revealed")

    def test_auto_mode_reveals_after_every_player_answers_then_advances(self):
        room = QuizRoom("ABCDE", validate_quiz(VALID_QUIZ), "host-secret", "auto")
        first_player = room.add_player("First")
        second_player = room.add_player("Second")
        timers = []
        clock = [100.0]

        class CapturedTimer:
            def __init__(self, interval, function, args=(), kwargs=None):
                self.interval = interval
                self.function = function
                self.args = args
                timers.append(self)

            def start(self):
                pass

        with patch("app.threading.Timer", CapturedTimer), patch(
            "app.time.monotonic", side_effect=lambda: clock[0]
        ):
            room.host_action("host-secret", "start")
            room.submit_answer(first_player, 1)
            self.assertEqual(room.public_state()["phase"], "question")

            room.activate_powerup(second_player, "second_chance")
            room.submit_answer(second_player, 0)
            state = room.public_state(first_player)
            self.assertEqual(state["phase"], "question")
            self.assertEqual(
                state["second_chance_seconds"], SECOND_CHANCE_WINDOW_SECONDS
            )
            self.assertEqual(len(timers), 1)
            self.assertEqual(timers[0].interval, SECOND_CHANCE_WINDOW_SECONDS)

            room.submit_answer(second_player, 1)
            clock[0] += SECOND_CHANCE_WINDOW_SECONDS
            timers[0].function(*timers[0].args)

            state = room.public_state(first_player)
            self.assertEqual(state["phase"], "revealed")
            self.assertEqual(state["correct_answer"], "Right")
            self.assertEqual(room.players[first_player]["score"], 1)
            self.assertEqual(room.players[second_player]["score"], 1)
            self.assertEqual(timers[1].interval, AUTO_ADVANCE_SECONDS)

            timers[1].function(*timers[1].args)
            self.assertEqual(room.public_state()["phase"], "completed")

    def test_auto_mode_does_not_open_second_chance_window_unless_activated(self):
        room = QuizRoom("NOPOWER", validate_quiz(VALID_QUIZ), "host-secret", "auto")
        player_id = room.add_player("Player")
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
            room.submit_answer(player_id, 1)

        self.assertEqual(room.public_state()["phase"], "revealed")
        self.assertEqual(len(timers), 1)
        self.assertEqual(timers[0].interval, AUTO_ADVANCE_SECONDS)

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

    def test_player_can_activate_a_powerup_once_through_the_api(self):
        status, room = self.request("/api/rooms", {"quiz": VALID_QUIZ})
        self.assertEqual(status, 201)
        status, player = self.request(
            "/api/join",
            {"room_code": room["room_code"], "name": "Player"},
        )
        self.assertEqual(status, 201)
        self.request(
            "/api/host/action",
            {
                "room_code": room["room_code"],
                "host_token": room["host_token"],
                "action": "start",
            },
        )

        payload = {
            "room_code": room["room_code"],
            "player_id": player["player_id"],
            "powerup": "call_friend",
        }
        status, result = self.request("/api/powerup", payload)
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        status, state = self.request(
            "/api/state?room={}&player={}".format(
                room["room_code"], player["player_id"]
            )
        )
        self.assertEqual(status, 200)
        self.assertEqual(state["used_powerups"], ["call_friend"])
        self.assertEqual(len(state["powerups"]), 4)

        status, result = self.request("/api/powerup", payload)
        self.assertEqual(status, 409)
        self.assertIn("already used", result["error"])

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

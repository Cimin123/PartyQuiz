# Quiz Night

A small, local-network party quiz. The host chooses a JSON or CSV quiz file, starts a room, and shares the room code. Players join from a browser on the same Wi-Fi. Everyone's screen updates together. Choose host-controlled play (manual reveal and advance) or automatic play (reveal after everyone answers, then advance after three seconds; Second Chance can add a five-second retry window after a wrong guess).

## Run it

1. Install Python 3.10 or newer if it is not already installed.
2. (Optional) Install dependencies with `python -m pip install -r requirements.txt`. The app currently has no third-party dependencies.
3. From this folder, run `python app.py` (or `py app.py` on Windows).
4. Open `http://localhost:8000` on the host computer.
5. Give other players the LAN address printed in the terminal and the room code.

The server listens on the local network so other devices can connect. If Windows Firewall asks, allow Python on **Private networks** only. Everyone must be on the same Wi-Fi/network. Room snapshots are stored locally so an interrupted game can be restored.

## Crash recovery

The server saves each room to `snapshots/<room-code>.json` after changes and every three minutes. It also writes a final snapshot when stopped normally. On the next start, saved rooms are restored automatically, including the current question, scores, answers, and game mode. The snapshot includes a readable leaderboard, current question number, and each player's reconnect code.

Players can save the personal reconnect code shown in their game screen, and the host can see every player's code beside their name in the lobby and scoreboard. To reclaim a seat after a restart, enter the room code and personal reconnect code on the join screen. The snapshot directory also contains host and player recovery credentials; keep it private and do not share or commit it.

To use another port, run `python app.py 9000`.

## Quiz JSON format

Start with [sample_quiz.json](sample_quiz.json). A quiz has a title and 1–100 questions. Each question has 2–6 answer objects and exactly one answer marked `"correct": true`.

```json
{
  "title": "Movie Night",
  "questions": [
    {
      "question": "Who directed Jaws?",
      "answers": [
        { "text": "Steven Spielberg", "correct": true },
        { "text": "George Lucas" },
        { "text": "Martin Scorsese" }
      ]
    }
  ]
}
```

Each correct answer earns one point. In automatic mode, all players need to answer before the result is revealed and the three-second transition begins, unless the host chooses **Reveal now**. An early reveal scores submitted answers and leaves unanswered players at zero. When the game ends, both the host and players can return to the start screen to create or join another game.

Answer choices are shuffled once when a room is created. The same order is used for everyone and is preserved if the room is restored from a snapshot.

Each player gets five personal power-ups, each usable once per game: 50:50 hides half the answer choices (all incorrect) for that player, Scout shows anonymous counts of other players' submitted answers, Call a friend is an honor-rule prompt, Second Chance marks a player's first guess wrong and lets them make one different guess without revealing the correct choice, and Memory test hides answer text for one other random player until the reveal, leaving only the choice letters visible. In automatic mode, a five-second retry window opens once everyone has answered if a player using Second Chance still needs their retry.

Every fifth question gets one randomly selected modifier, shown to both the host and players. Modifiers include a high-five with the birthday celebrant, complimenting someone on the opposing team, consulting Paulina before answering, a consensual treat for the team answering correctly (non-alcoholic drink or snack), no talking, the phone captain (only the person holding or closest to the phone may answer), a 15-second timer, triple points, or a two-point penalty for a wrong answer.

## Quiz CSV format

The host can also import a comma-separated CSV with headers `Pytanie` (or `Question`), `A`–`E`, and `Poprawna odp` (or `Correct answer`). An optional `Kategoria`/`Category` column is shown with each question. An optional `Include` column can exclude rows by setting it to `0`, `false`, `no`, or `nie`. Empty answer cells become `x`. Rows without a question or a correct answer matching exactly one choice are skipped and reported in the lobby. JSON import remains supported.

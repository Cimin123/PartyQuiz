# Quiz Night

A small, local-network party quiz. The host chooses a JSON or CSV quiz file, starts a room, and shares the room code. Players join from a browser on the same Wi-Fi. Everyone's screen updates together. Choose host-controlled play (manual reveal and advance) or automatic play (reveal when every player has answered, then advance after three seconds).

## Run it

1. Install Python 3.10 or newer if it is not already installed.
2. (Optional) Install dependencies with `python -m pip install -r requirements.txt`. The app currently has no third-party dependencies.
3. From this folder, run `python app.py` (or `py app.py` on Windows).
4. Open `http://localhost:8000` on the host computer.
5. Give other players the LAN address printed in the terminal and the room code.

The server listens on the local network so other devices can connect. If Windows Firewall asks, allow Python on **Private networks** only. Everyone must be on the same Wi-Fi/network. Rooms are stored in memory and disappear when the server stops.

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

Each correct answer earns one point. In automatic mode, all players need to answer before the result is revealed and the three-second transition begins. When the game ends, both the host and players can return to the start screen to create or join another game.

## Quiz CSV format

The host can also import a comma-separated CSV with headers `Pytanie` (or `Question`), `A`–`E`, and `Poprawna odp` (or `Correct answer`). An optional `Kategoria`/`Category` column is shown with each question. An optional `Include` column can exclude rows by setting it to `0`, `false`, `no`, or `nie`. Empty answer cells become `x`. Rows without a question or a correct answer matching exactly one choice are skipped and reported in the lobby. JSON import remains supported.

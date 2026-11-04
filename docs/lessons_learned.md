# Lessons learned

Real problems found while building and checking this project.

### Answering about the wrong company
- **Symptom:** With Tidewell selected, a question about Kitebridge's funding got Tidewell's numbers. No data leaked, but the answer misled.
- **Cause:** The agent was not told which company was selected.
- **Found by:** Reading the transcript of the first real-model run of `scripts/verify.py`.
- **Fix:** The prompt names the company and forbids answering about another. Check 5 now requires zero claims here.

### Revenue question used all 6 model calls
- **Symptom:** Asked for revenue, which no file states, the agent searched until the step limit and gave no final answer.
- **Cause:** Nothing told the model when to stop looking.
- **Found by:** The live test passed only on fallback wording; the run's `step_limit` status showed the real result.
- **Fix:** An efficiency rule and a note on the last allowed step. The live test now requires status `ok`.

### Speculation in the brief
- **Symptom:** A brief said the company "may need more capital soon".
- **Cause:** Citation checks prove a quote exists, not that the sentence built on it is supported.
- **Found by:** Reading a real brief during UI testing.
- **Fix:** A prompt rule: a risk must be shown by the materials or be a stated gap. The program does not enforce this.

### Delete did nothing, then sent two requests
- **Symptom:** The delete link did not respond. After a first fix, cancelling then confirming sent two DELETE requests.
- **Cause:** The browser blocked native `confirm()`, which returned false with no error. The first fix relied on the dialog's `close` event, which can arrive after the dialog reopens.
- **Found by:** User testing and the browser console. An earlier walkthrough missed it by replacing `confirm()` with an automatic yes. A click and request log showed the double request.
- **Fix:** A dialog inside the page, answered from its buttons. A test bans native dialogs in the UI script.

### SQLite reused IDs
- **Symptom:** A replaced file's new document had the old `document_id`.
- **Cause:** Without `AUTOINCREMENT`, SQLite can reuse a deleted last row's ID, so an old citation could point at different text.
- **Found by:** Trying file replacement by hand and reading the response.
- **Fix:** `AUTOINCREMENT` on the ID columns, plus a regression test.

### API key in git history
- **Symptom:** A real key sat on a commented line of `.env.example`, in every commit after it was added.
- **Cause:** The key got into the tracked template, and a `git commit -a` included it unread.
- **Found by:** The audit before the first public push, which searched all of history. The key never reached GitHub.
- **Fix:** History rewritten, old objects purged, every object and a fresh clone searched, key replaced. No automatic secret scan is set up yet.

### Invalid key reported as "Which company do you mean?"
- **Symptom:** With a dead key, an unrelated question got "Which company do you mean?" instead of an error, and the upload dialog said no company was recognized.
- **Cause:** Both model checks swallowed failures and reported a normal result. The server also reads `.env` only at startup, so it kept the old key.
- **Found by:** Checking the running server after replacing the key.
- **Fix:** Both now show a clear error, such as "Model call failed: API key invalid or missing. Check .env and restart the server." Uploads can still go ahead.

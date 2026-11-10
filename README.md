# Zendesk macro edit audit

This tool shows how often agents change a macro's text before they send it.

It looks at the last 30 days of ticket updates in your Zendesk account, up to the moment the run starts. Anything saved after that waits for the next run. Every time an agent applied a macro, it compares the comment they saved with the text the macro inserts. Then it writes:

- **report.html**, a readable report you can open in any browser or share
- **macro_uses.csv**, one row per macro use (who, which macro, public or internal, edited or not, percent of the macro edited)
- **macros.csv**, totals per macro
- **agents.csv**, totals per agent
- **audit.json**, everything above in one file for other tools to read

### audit.json

`audit.json` is for tools that suggest changes to macros, list unused macros to delete, or remove personal details before sending text anywhere else. It lists every macro in the account, including inactive and personal ones and those nobody used, with its full text. It also lists every use with the full set of changes the agent made, and the names and emails of the agents and the customers who asked for help on those tickets.

**It contains personal data**, such as customer names, emails, and the text of replies. Only your user account on this computer can open it. Don't share it or upload it anywhere until those details are removed.

In a diff, `op` is `equal`, `delete`, `insert`, or `macro`. A `macro` step is the text of another macro applied in the same update, named by `macro_id`. Words are joined by single spaces, so line breaks aren't kept.

The report covers:

- the percentage of macro uses sent edited and kept intact
- the share of agents who edit macros
- which macros are edited most often, and which are usually kept intact
- public replies and internal notes, counted separately
- how much of each macro gets edited, on average and per use
- every single use, with a view of exactly what the agent changed and a link to the ticket. Long stretches of added or removed text are shortened to "… N more words".

Here's the top of a report made from the tool's sample data. Below this part, the full report goes on to list agents, every single use with its changes, and the uses that couldn't be compared. In the report, hover over any underlined label to see what it means.

![The top of a macro edit report. It shows 447 macro uses, 12% sent edited, 88% kept intact, and 83% of agents editing, then lists which macros are edited most often and kept intact most often, a breakdown by public reply and internal note, and a table of every macro.](docs/report.png)

It runs on macOS with Python 3.9 or newer. There's nothing to install.

**Windows and Linux aren't available at the moment.** The tool keeps sign-in details in the macOS keychain, and it won't sign in to Zendesk on a system where it would have to store them in plain text. On those systems, only `--sample` works.

## Try it without Zendesk

```bash
python3 zd_macro_audit.py --sample
```

This runs on made-up data and prints where the report was saved.

## One-time Zendesk setup

You need a Zendesk **admin** for this. It takes about two minutes.

There are two ways to sign in.

- **OAuth** is Zendesk's recommended method. It needs the setup below.
- **An API token** skips that setup, if the admin already has one. See [Sign in with an API token](#sign-in-with-an-api-token).

For the OAuth setup, your Zendesk account needs an "OAuth client", which is a small record that tells Zendesk this tool is allowed to ask for access.

1. Sign in to Zendesk as an admin.
2. Open **Admin Center**. Click the four-squares icon in the top bar, then **Admin Center**.
3. In the left sidebar, click **Apps and integrations**, then **APIs**, then **OAuth Clients**. (In some accounts this is **Apps and integrations > APIs > Zendesk API**, then the **OAuth Clients** tab.)
4. Click **Add OAuth client**.
5. Fill in the form.
   - **Name**. Something like `Macro edit audit`. Agents see this name when they approve access.
   - **Description**. Optional.
   - **Company**. Your company name.
   - **Unique identifier**. Zendesk fills this in from the name, for example `macro_edit_audit`. Copy it. This is the client ID the tool asks for.
   - **Client kind**. Choose **Confidential**.
   - **Redirect URLs**. Enter exactly `http://localhost:8765/callback`
6. Click **Save**.
7. Zendesk now shows a **Secret**. Copy it right away, because Zendesk only shows it once. If you lose it, click **Regenerate** on the same page to get a new one.

You'll paste the unique identifier and the secret into the tool the first time you run it. See [Run it](#run-it). The tool saves the unique identifier in `config.json` and the secret in the macOS keychain, so you only do this once. Keep the secret private, like a password.

## Run it

```bash
python3 zd_macro_audit.py
```

The first time, it asks for your Zendesk subdomain. For `acme.zendesk.com`, that's `acme`. Pasting `acme.zendesk.com` or a Zendesk link works too. If what's left isn't just letters, numbers and hyphens, the tool stops and says so. Then it asks how you want to sign in. Choose OAuth, and it asks for two more things.

1. The unique identifier from step 5.
2. The secret from step 7. Nothing shows on screen while you paste or type it, so it stays private. Paste it and press Enter.

It saves the unique identifier in `config.json` in this folder and the secret in the macOS keychain. Then it opens your browser so you can sign in to Zendesk and click **Allow**. Sign in as an admin, because only admins can read every ticket's history. After that, the browser tab says you can close it, and the tool carries on in the terminal.

Later runs reuse the saved sign-in and go straight to work. macOS may ask you to allow access to the keychain, or to unlock it.

If you ran an earlier version, the tool moves any secrets it finds in `config.json` to the keychain. It doesn't use `.tokens/` any more, so it warns you until you delete that folder. You'll sign in through the browser once more.

### Sign in with an API token

If the admin already has a Zendesk API token, the tool can use it instead of OAuth. There's no OAuth client to set up and no browser step.

```bash
python3 zd_macro_audit.py --api-token
```

It asks for the admin's email and the token. It saves the email in `config.json` and the token in the macOS keychain. Like the OAuth secret, the token doesn't show on screen as you paste it. Later runs use the saved token without the flag. The token must belong to an admin, because only admins can read every ticket's history.

To find or make a token, go to **Admin Center > Apps and integrations > APIs > API tokens**. Zendesk only shows a new token once, so copy it right away. If that page says token access is off, turn it on there.

If both an API token and an OAuth client are saved, the token wins. Run with `--oauth` to use OAuth instead.

### Options

| Option | What it does |
| --- | --- |
| `--days 60` | Look back 60 days instead of 30. |
| `--out some/folder` | Save reports somewhere other than `./reports`. |
| `--api-token` | Sign in with an admin's existing API token instead of OAuth. |
| `--oauth` | Sign in with OAuth even if an API token is saved. |
| `--login` | Sign in to OAuth again, for example as a different admin. |
| `--sign-out` | Remove this Zendesk account's saved sign-in, OAuth secret and API token from the keychain. |
| `--sample` | Use made-up data instead of Zendesk. |
| `--config other.json` | Use a different settings file, for example one per Zendesk account. |
| `--workers 2` | Read 2 tickets at a time instead of 4. Lower this if other tools share your Zendesk API limit. |

You can also pass settings through environment variables instead of `config.json`. Use `ZENDESK_SUBDOMAIN` with either `ZENDESK_CLIENT_ID` and `ZENDESK_CLIENT_SECRET` for OAuth, or `ZENDESK_EMAIL` and `ZENDESK_API_TOKEN` for an API token. Settings from the environment are never saved.

### How long it takes

The tool reads the history of every ticket updated in the period, one request per ticket. So the run time depends on how many tickets were updated and on your Zendesk plan's API limit.

| Tickets updated in 30 days | Team plan (200 requests a minute) | Enterprise plan (700 requests a minute) |
| --- | --- | --- |
| 1,000 | about 5 minutes | about 2 minutes |
| 10,000 | about 50 minutes | about 15 minutes |

The tool prints its progress and an estimate of the time left. If Zendesk says the tool is going too fast, it waits and tries again on its own. Other tools that use your Zendesk API share the same limit, so a run during quiet hours goes faster.

### When Zendesk stops answering

If a request fails because of a network error, or because Zendesk is busy or has a server error, the tool waits and tries again, several times. A ticket that still fails is set aside and tried once more at the end of the run. Any ticket still unread after that is listed in the terminal summary, in a warning at the top of the report, and in `unread_tickets.csv` (ticket ID and link). Running again usually picks them up.

If 20 tickets in a row fail, Zendesk has probably stopped answering, so the run stops early. It still writes a report from the tickets it read, marked incomplete at the top. If no tickets could be read, or the macros' text couldn't be fetched, there's no report.

Tickets that were deleted, or that the sign-in isn't allowed to see (for example, restricted to a group), are skipped. The summary and the report count them in a note. This can explain why the report finds fewer uses than the "Zendesk count" column.

For scripts, the tool exits with 0 when the run finished, even if a few tickets were left unread. It exits with 1 when the run stopped early, when there's no report, or when it stopped with an error, such as a missing or invalid setting or a sign-in Zendesk didn't accept.

## Zendesk endpoints used

All requests are read-only.

| Endpoint | What it's for |
| --- | --- |
| `GET /api/v2/incremental/tickets/cursor` | Lists every ticket updated in the period, and who asked for help on each. |
| `GET /api/v2/tickets/{id}/audits` | Each ticket's history. A "macro reference" event shows a macro was applied, and the "comment" event in the same update shows what was sent. |
| `GET /api/v2/macros?include=usage_30d` | Every macro, active or not, with its text and Zendesk's own 30-day usage count. For an admin this includes agents' personal macros. |
| `GET /api/v2/macros/{id}` | Tries to get macros that are missing from the list. |
| `GET /api/v2/dynamic_content/items` | Expands `{{dc.*}}` placeholders. Only called if a macro uses one. |
| `GET /api/v2/users/show_many` | Names and emails of agents, and of the customers who asked for help on tickets where a macro was used. |

Zendesk's usage count is a check on the tool. In the report's macro table, "Zendesk count" should be close to "Uses". A big gap means the tool missed some uses. The count needs the Support Professional plan or higher. On other plans the column is left out.

## How "edited" is decided

A use counts as **kept intact** when the macro's text appears unchanged anywhere in the saved comment. Agents often use a macro as part of a longer reply, so a greeting, signature or anything else added before or after it isn't an edit. These uses are marked "with text added" in the report, and `text_added` in `macro_uses.csv`.

"Unchanged" allows for these exceptions.

- **Placeholders** like `{{ticket.requester.first_name}}` match what Zendesk filled in, up to the limits below.
- **Dynamic content** like `{{dc.sign_off}}` matches any of its language versions.
- **Liquid blocks** like `{% if %}...{% else %}...{% endif %}` match any of their branches exactly, or nothing if there's no `else`. Rewriting a branch counts as an edit.
- **Spacing, bold, italics, links, images, list bullets, curly quotes and invisible characters** are ignored, because the editor changes these on its own.

### How much a placeholder can fill

Zendesk doesn't record what it filled in, so the tool limits how many words each placeholder can stand for. Without a limit, a paragraph typed next to a placeholder would look like part of it.

| Placeholder name contains | Words it can fill |
| --- | --- |
| `comment`, `description` | 200 |
| `signature` | 40 |
| `url`, `link`, `title`, `subject` | 15 |
| A custom field, `ticket.ticket_field_<id>` | 15 |
| `email` | 8 |
| `phone` | 6 |
| `first_name`, `last_name` | 3 |
| `name` | 6 |
| Ends in `id` | 3 |
| Anything else | 5 |
| A `{% for %}` loop | 30 |
| `{{dc.*}}` that couldn't be read | 100 |

A hyphenated word or one with an apostrophe, like `Mary-Jane` or `O'Neil`, counts as one word here.

The limits lean toward flagging. A value longer than its limit counts as an edit even if the agent didn't touch it, because that's better than missing text an agent added. Added text that fits within a limit can still pass as part of the placeholder. To change a limit, edit `GAP_LIMITS` in `zdaudit/compare.py`.

Every other change inside the macro's text counts as an edit, however small, including text added in the middle of it. 

### How much of a macro was edited

Each use gets a **percent of the macro edited**, from 0% (intact) to 100%. It counts the macro's words that were removed or changed, plus words inserted inside the macro, as a share of the macro's words.

| What the agent did to a 20-word macro | Percent edited |
| --- | --- |
| Nothing, or added a greeting or signature around it | 0% |
| Changed one word | 5% |
| Inserted 3 words in the middle | 15% |
| Replaced 2 words with 6 words | 30% |
| Inserted 50 words in the middle | 100% |
| Deleted all of it | 100% |

A changed word counts once. Words a placeholder can fill and text added around the macro don't count, and a placeholder value over its limit only counts for the extra words. The percentage stops at 100%, so an insertion longer than the macro reads the same as a full rewrite.

In the report, click a macro to see each of its uses, with this percentage and a link to the ticket. The macro table shows two averages. **Avg % of macro edited** covers all compared uses, with intact ones as 0%, so it shows the overall drift. **Avg when edited** covers only edited uses, so it shows how big the edits are when they happen.

If a macro adds text but the agent saved the update with no comment, they deleted all of the macro's text. That counts as edited, with 100% of the macro edited. The report lists these uses as "No comment".

If an agent applied several macros in one update, each one is judged on its own, and their text can appear in any order. Each macro counts as one use. A word in the comment can only be credited to one macro, so two macros that share a sentence need that sentence to appear twice to both count as kept intact. When only one copy appears, the macro left without it counts as edited.

### Uses that aren't counted

Some uses can't be judged, so they're left out of the percentages and listed separately in the report.

- **Macro has no comment text.** The macro only sets fields or tags.
- **Macro is only placeholders.** All of its text is filled in by Zendesk, for example a macro that's just `{{dc.reply}}` when that item can't be read, so any comment could match it.
- **Macro deleted or unreadable.** The macro no longer exists, so its text is unknown.
- **Macro changed since use.** Zendesk only gives the current version of a macro. If someone edited the macro during the period, older uses that don't match it can't be judged.

## Privacy and access

- The tool only reads from Zendesk. It never changes anything in your account.
- With OAuth, the tool asks Zendesk for **read** access only. Your sign-in and the OAuth secret are saved in the macOS keychain, under `zd-macro-audit`, and never in this folder. Run with `--sign-out` to remove them. To cut off access completely, delete the OAuth client in Admin Center.
- An API token can't be limited to reading. It has the full rights of the admin who owns it, so anyone holding it can read and change everything that admin can. The tool itself only reads. Where you can, use OAuth instead, since it only gets read access. The tool saves the token in the macOS keychain, and `--sign-out` removes it. To cut off access, delete the token in Admin Center.
- The keychain keeps secrets encrypted and out of this folder, so they don't travel with a zip, a backup or a shared folder. It doesn't protect against other programs running under your account, which can ask the `security` command for them.
- The report contains text from ticket comments, both public replies to customers and internal notes, which can include customer details. The report and CSVs contain agent names and emails. `audit.json` also contains customer names and emails. Treat them as sensitive, and check before you forward them or upload them anywhere.

## Files

| File | What it is |
| --- | --- |
| `zd_macro_audit.py` | The command you run. |
| `zdaudit/zendesk.py` | OAuth and API token sign-in, and the Zendesk API calls. |
| `zdaudit/keychain.py` | Saves and reads secrets in the macOS keychain. |
| `zdaudit/compare.py` | Works out which words of a comment came from which macro, and measures how much of it was edited. |
| `zdaudit/analyze.py` | Turns raw data into per-use results and totals. |
| `zdaudit/report.py` | Writes the HTML report and CSV files. |
| `zdaudit/assets/` | The report's styles and script, which are copied into each report. |
| `zdaudit/sample.py` | The made-up data behind `--sample`. |
| `tests/` | Tests. Run them with `python3 -m unittest`. |

## License

This project uses the MIT License. See [LICENSE](LICENSE).

Just getting started? Read the [guide](docs/getting-started.md) for step-by-step instructions, from getting the tool to reading your first report.

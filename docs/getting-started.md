# Getting started

This guide takes you from getting the tool to reading your first report. It works whether you ran an earlier version of the tool or this is your first time.

You'll need:

- A Mac. The tool keeps your sign-in in the macOS keychain, so it doesn't sign in on Windows or Linux.
- Python 3.9 or newer. To check, open the Terminal app and run `python3 --version`. If macOS offers to install developer tools, accept and wait for it to finish.
- About 10 minutes.

Every command below goes in the Terminal app. Paste one at a time and press Enter.

## 1. Get the latest version

### If you already have the tool

Go to the folder you ran it from before, then pull the latest version. For example, if it's in `zd-macro-audit` in your home folder:

```bash
cd ~/zd-macro-audit
```

```bash
git pull
```

Use this same folder, because it holds your `config.json` with your settings.

If `git pull` says "not a git repository", that folder was downloaded rather than cloned. Clone a fresh copy as in the next section, then copy your `config.json` from the old folder into the new one. Delete the old folder once the new one works.

### If this is your first time

Clone the tool into your home folder and go into it:

```bash
cd ~ && git clone https://github.com/balevine/zd-macro-audit.git
```

```bash
cd ~/zd-macro-audit
```

### Updating later

To get a newer version at any time, go to the folder and pull again:

```bash
cd ~/zd-macro-audit && git pull
```

Your settings, your saved sign-in and your past reports are kept.

## 2. Sign in and run a 1 day test

Run the tool on just the last day, which takes a minute or two:

```bash
python3 zd_macro_audit.py --days 1
```

**If you ran the tool before**, it finds your OAuth client secret in `config.json` and moves it to the macOS keychain on its own. You'll see:

```
Saved secrets in the macOS keychain and other settings in config.json. Use --sign-out and delete that file to start over.
```

After that, `config.json` keeps your other settings, such as your subdomain and the OAuth client's unique identifier. The secret is no longer saved in plain text anywhere in the folder.

The tool no longer reads the sign-in saved by earlier versions, so you sign in to Zendesk once more, just this time. The tool opens your browser. Sign in as an admin and click **Allow**. When the tab says "Signed in. You can close this tab and go back to the terminal", close it. The terminal shows:

```
Signed in as Your Name (you@example.com).
```

The tool saves this new sign-in in the keychain, so later runs skip the browser.

**If this is your first time**, the tool asks for your Zendesk subdomain and how you want to sign in. Press Enter to choose OAuth. Then paste the OAuth client's unique identifier and secret, and sign in through the browser as described above. Nothing shows on screen while you paste the secret. That's normal. Press Enter after pasting. The README's [One-time Zendesk setup](../README.md#one-time-zendesk-setup) section explains where to find both.

macOS may ask you to allow access to the keychain, or to unlock it. Enter your Mac password and click **Always Allow**, so it doesn't ask on every run.

If the terminal warns that your account isn't an admin, sign in again as an admin with `python3 zd_macro_audit.py --days 1 --login`. Agents can't see every ticket, so their results are incomplete.

The tool then prints its progress and a summary that ends like this:

```
Report: /Users/you/zd-macro-audit/reports/2026-11-07_101500/report.html
Files:  /Users/you/zd-macro-audit/reports/2026-11-07_101500
```

If you see "Network error … Retrying", Zendesk was slow to answer. The tool waits up to 2 minutes for each request, then tries again on its own, so let it keep going.

### Check that the secret moved

This should print `0`, meaning the secret isn't in `config.json` any more:

```bash
grep -c client_secret config.json
```

Run the 1 day test once more. This time there's no "Saved secrets" line and no browser, because the tool reads the secret and your sign-in from the keychain:

```bash
python3 zd_macro_audit.py --days 1
```

## 3. Remove the old sign-in file

Earlier versions saved a sign-in in a `.tokens` folder inside the tool's folder, in plain text. The tool doesn't use it any more, and it warns you on every run until it's gone:

```
Warning: /Users/you/zd-macro-audit/.tokens holds an old sign-in in plain text. The tool no longer uses it, so please delete that folder.
```

Delete it:

```bash
rm -rf .tokens
```

If you don't see that warning, there's no old sign-in to remove.

## 4. Look at the report

Open the newest report in your browser:

```bash
open "$(ls -d reports/*/ | tail -1)report.html"
```

You can also open the `reports` folder in Finder and double-click `report.html` in the newest folder. Each run gets its own folder, named by date and time.

The report starts with totals: how many times macros were used, how many were sent edited and how many agents edit. Below that it lists the macros edited most often, public replies and internal notes, every macro, every agent, and every single use with a view of exactly what changed and a link to the ticket. Hover over any underlined label to see what it means.

The same folder has CSV files you can open in Excel, Numbers or Google Sheets. The README's opening section describes each one.

The report contains ticket text and agent names and emails, which can include customer details. Treat it as sensitive, and check before you forward or upload it.

## 5. Run the full report

Once the 1 day test works, run the full report, which covers the last 30 days:

```bash
python3 zd_macro_audit.py
```

This takes longer, depending on how many tickets you have. The README's [How long it takes](../README.md#how-long-it-takes) section has an estimate.

## If something goes wrong

- **"This tool only signs in to Zendesk on macOS for now"**: you're not on a Mac. Only `--sample` works elsewhere.
- **The browser doesn't open**: copy the link the terminal prints under "paste this link into your browser" and open it yourself.
- **"Zendesk didn't grant access" or "Zendesk rejected the token request"**: you clicked something other than **Allow**, or the OAuth client's settings don't match. Check the client in Admin Center against the README's [One-time Zendesk setup](../README.md#one-time-zendesk-setup), then start over as described below, so the tool asks for the identifier and secret again.
- **To sign in as a different admin**: run the tool with `--login`.
- **A keychain error**: open the Keychain Access app and check that your login keychain is unlocked, then run the tool again.
- **To start over completely**: run `python3 zd_macro_audit.py --sign-out` and delete `config.json`. The next run asks for everything again.

# Judge demo guide

**One-liner:** After a hurricane, responders have thousands of aerial photos and hours, not days, to decide where to send search teams. This platform turns drone or aerial imagery into a reviewed, mapped, prioritised task list for rescue crews, with Claude doing the first pass and humans making the call.

## Before you present (10 minutes)

1. Connect Claude. The app uses the Claude API, which is billed through a Claude Console account, not the Claude.ai app. A Max plan includes monthly API credit you can link; a Pro plan doesn't include API usage. Choose one:
   - **Sign in (no key to manage):** `brew install anthropics/tap/ant`, then `ant auth login` and pick your Console organisation in the browser.
   - **API key:** create one at console.anthropic.com, then put it in `disaster_assessor/.env` as `ANTHROPIC_API_KEY=sk-ant-...`.
2. Start everything:
   ```bash
   cd disaster_assessor && ./start-demo.sh
   ```
   On first run this creates the **Hurricane Ian – Fort Myers Beach, FL** incident:
   - Claude assesses three real NOAA aerial mosaics (about 1–2 minutes and roughly $1–2 of API usage).
   - Real OpenStreetMap building outlines are imported for those areas.
   - Two crews are created and tasks are made for the worst-hit buildings.

   The browser opens at http://127.0.0.1:8000.
3. Open `disaster_assessor/demo/noaa-ian/` in Finder so `matlacha.tif` is ready to drag in during the demo.
4. Optional phone view: open http://127.0.0.1:8000/field/ in a second browser window. Use the browser's device toolbar (Chrome: ⌥⌘I, then the phone icon) to show it at phone size.
5. Rehearse once. Check that internet works, since map tiles and Claude need it.

## The 5-minute script

| Time | Show | Say |
| --- | --- | --- |
| 0:00 | **Incidents** page → open Hurricane Ian | "Two days after Hurricane Ian, NOAA flew Fort Myers Beach. These are their real photos." |
| 0:30 | **Console**: the viewport with boxes; step through with the D-pad | "Claude found every building, drew a box and rated damage on the FEMA/xBD scale. Red is destroyed, orange major, yellow minor, green intact." |
| 1:00 | Drag **matlacha.tif** onto the viewport → **Run assessment** | "Here's a neighbourhood it hasn't seen. Live, from the raw GeoTIFF." (It finishes in about 30–60 s; talk over the progress bar.) |
| 1:45 | **Review** (`R`): select a red box | "Every box comes with Claude's reasoning, here 'only a slab remains'. But it's a first pass: a responder confirms or corrects it." Press `C` to confirm, then `2` on another box to correct it. Press `D` and draw a missed building. "Every decision is logged." |
| 2:45 | **Map** | "Because the imagery is georeferenced, each building lands at its real position. The coloured outlines are real OpenStreetMap buildings, so a house photographed twice counts once." Open the layers button and turn on the heatmap. |
| 3:30 | **Tasks & crews** → **Plan route** on the map | "Destroyed and major buildings become search tasks. Assign a crew and it gets a visiting order, worst first." |
| 4:00 | **Field app** (phone window): open a task → **Needs USAR** + note → Send | "Crews work offline in the field. When they report, the coordinator sees it live." Switch back: a toast and the task has moved. |
| 4:30 | **Export** → Situation report (PDF) and a share link | "One click gives the EOC a briefing PDF, GIS files, or an expiring read-only link for partner agencies." |
| 5:00 | Close | "First pass in minutes, humans in control, every decision traceable." |

## Questions judges will ask

- **How accurate is it?** "Claude is a general vision model, not trained specifically on xBD. On these images it's good at clearly destroyed versus intact and less sure about minor damage. That's why every box goes through human review, and the PDF flags unreviewed output. The platform also supports purpose-trained YOLO models, and there's an xBD training pipeline in `training/`." Don't quote an accuracy number; we haven't measured one.
- **Why Claude instead of a trained model?** "It works today on any imagery (drone, aerial, phone photos) with no GPU or training data, and it explains its reasoning, which speeds up review. Trained models are cheaper at scale, and the app supports both."
- **What does it cost?** "Roughly 5–15 cents per photo and about 50 cents per aerial mosaic with Claude Opus. Each run records its actual cost in the run history." (Open the console's run history to show it.)
- **Where do the locations come from?** "GeoTIFF georeferencing here. For drone photos it projects from the drone's GPS, altitude and camera angle. Phone photos use the camera's GPS."
- **Privacy and security?** "It's self-hosted, with role-based accounts, an audit log, and expiring share links that hide crew and reviewer names. Imagery goes only to the Claude API when Claude mode is used." (Sign-in is turned off for this demo with `DRA_NO_LOGIN`, which only works on localhost.)
- **Does it work offline?** "The field app does, and it syncs when crews are back in coverage. The analysis needs a connection for Claude, or a local GPU model."

## If something goes wrong

- **Claude is slow or rate-limited:** skip the live upload. The seeded incident already has Claude results.
- **No internet:** map tiles won't load. Switch the map to **Off** and show the imagery overlays and boxes alone.
- **Wrong app opened:** use http://127.0.0.1:8000. The old Streamlit demo runs on a different port.
- **Start fresh:** stop the server, delete `disaster_assessor/data/` (this removes everything), then run `./start-demo.sh` again.

## Data credits

Imagery: NOAA National Geodetic Survey Emergency Response Imagery, Hurricane Ian, 30 Sep 2022 (U.S. Government work; see `demo/noaa-ian/README.md`). Building outlines © OpenStreetMap contributors.

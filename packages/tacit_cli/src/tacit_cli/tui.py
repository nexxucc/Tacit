"""Textual candidate/evidence review; publication stays in the explicit confirm command."""

import asyncio
import json
from difflib import unified_diff
from uuid import uuid4

from tacit_contracts.models import ClarificationAnswer, PrepareDraft, TeachingView
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button, Footer, Header, Static, TextArea

from tacit_cli.teaching import request, route


def review_teaching(repo: str, payload: object) -> None:
    view = TeachingView.model_validate(payload).model_dump(mode="json")

    class Review(App[None]):
        TITLE = "Tacit — human teaching review"
        BINDINGS = [("q", "quit", "Close")]
        CSS = "TextArea {height: 18;} #details {height: auto;} Button {margin: 1;}"

        def compose(self) -> ComposeResult:
            yield Header()
            with VerticalScroll():
                baseline = json.dumps(view.get("baseline", []), indent=2).splitlines()
                proposed = json.dumps(view.get("candidates", []), indent=2).splitlines()
                difference = "\n".join(
                    unified_diff(
                        baseline,
                        proposed,
                        fromfile="Confirmed baseline",
                        tofile="Proposed alternatives",
                        lineterm="",
                    )
                )
                yield Static(difference, id="diff", markup=False)
                yield Static(json.dumps(view, indent=2), id="details", markup=False)
                candidates = view.get("candidates", [])
                candidate = candidates[0]["id"] if candidates else ""
                initial = {
                    "operation_id": str(uuid4()),
                    "expected_version": view["version"],
                    "candidate_id": candidate,
                    "answers": {},
                    "accept_assumptions": False,
                }
                yield Static(
                    "Review alternatives, citations, assumptions and scenario outcomes above. Edit clarification JSON below.",
                    markup=False,
                )
                yield TextArea(json.dumps(initial, indent=2), id="answers")
                yield Button("Save clarification", id="save")
                yield Button("Prepare draft for separate confirmation", id="draft")
                yield Static("", id="result", markup=False)
            yield Footer()

        async def on_button_pressed(self, event: Button.Pressed) -> None:
            try:
                if event.button.id == "save":
                    body = ClarificationAnswer.model_validate_json(
                        self.query_one("#answers", TextArea).text
                    )
                    updated = await asyncio.to_thread(
                        request,
                        "POST",
                        route(repo, str(view["id"])) + "/answers",
                        body.model_dump(mode="json"),
                    )
                else:
                    draft = PrepareDraft(
                        operation_id=str(uuid4()), expected_version=int(view["version"])
                    )
                    updated = await asyncio.to_thread(
                        request,
                        "POST",
                        route(repo, str(view["id"])) + "/draft",
                        draft.model_dump(mode="json"),
                    )
                view.update(TeachingView.model_validate(updated).model_dump(mode="json"))
                self.query_one("#details", Static).update(json.dumps(view, indent=2))
                self.query_one("#diff", Static).update(
                    "\n".join(
                        unified_diff(
                            json.dumps(view.get("baseline", []), indent=2).splitlines(),
                            json.dumps(view.get("candidates", []), indent=2).splitlines(),
                            fromfile="Confirmed baseline",
                            tofile="Proposed alternatives",
                            lineterm="",
                        )
                    )
                )
                self.query_one("#result", Static).update(
                    "Saved. Publication requires tacit confirm DRAFT_ID --repo REPO_ID."
                )
                current = json.loads(self.query_one("#answers", TextArea).text)
                current.update(operation_id=str(uuid4()), expected_version=view["version"])
                self.query_one("#answers", TextArea).load_text(json.dumps(current, indent=2))
            except Exception as exc:
                self.query_one("#result", Static).update(str(exc))

    Review().run()

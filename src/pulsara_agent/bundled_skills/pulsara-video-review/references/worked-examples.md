# Worked examples

The times below illustrate choices based on the content and question.

## Review a long tutorial together

The user asks you to review a 12-minute dashboard tutorial and mark the key steps so they can follow along. This example uses visual evidence.

1. Start with a target-only overview. The video opens automatically in the sidebar. Locate the setup, chart and export sections.
2. Explore the setup with a local overview. When its main step is clear, use `annotate_media` on the relevant passage, for example: “Import the orders table and select the date column before opening the dashboard.” The user can already select this note and review the setup while you continue.
3. In the chart section, use overview on 240–250 s to locate the filter, then detail from 245.2 to 246.2 with a crop to read it. If the label shows October, annotate that visible state at 245.2–246.2: “The chart is filtered to October; check this selection when comparing the totals.”
4. The user selects that note and asks, “Did changing the filter also change the total?” Inspect the filter operation around 242–247 s with motion. Read the before/after amounts with short detail calls if needed, then annotate the operation's range with the observed change. Resume the chart section after answering.
5. Continue through the export section and the ending at 720 s. Add useful notes as each finding becomes clear. A passage may need only overview, only detail, or a combination with motion. Finish with a summary that points the user to the relevant annotated passages.

The notes give the user places to revisit and discuss throughout the review. If the task also asks about spoken explanations, read the corresponding transcript passages alongside the visual review.

## Read an amount at a supplied time

The user asks, “What amount is shown at 12.4 seconds?”

Use detail from 12.32 to 12.56, with a crop around the amount if its position is known. Read the returned pictures and actual times. If the value is clear, answer directly. If you need to find the amount on the screen first, use the full-frame detail, then tighten the crop around the relevant area.

If the tool returns one picture and a next call, check whether that picture already contains the amount. Continue only if another selected picture is needed.

## Check a save operation

The user asks, “Did the save near the end succeed?”

1. Use a local overview of the ending to find the save passage.
2. Inspect 83–90 s with motion. At 84 s the save dialog is visible; at 85 s an error appears.
3. If the text is readable, answer. If it is small, use detail from 85.1 to 85.8 with a crop around the message.
4. Report the actual outcome: for example, “At 85 s the app reports ‘Save failed: read-only folder.’” An optional timeline note can mark that finding.

For “When did the error first appear?”, concentrate the motion review around the transition and use its frame times. For “What does the error at 85 seconds say?”, start directly with detail.

## Look for a brief return to an earlier state

The user asks whether a menu opened and closed during 20–24 s. Review that interval with motion, following its pages through the relevant sequence. Inspect the opening, visible menu and return to the original screen in order. Use detail around the open menu only if its items matter to the answer.

For “Did this warning appear anywhere in 20–40 s?”, follow motion through the whole requested interval. Keep the remaining interval in view while reading pages. If a warning is found, use a short detail to read it when necessary. State the time and content that answer the user's question.

## Recover from an oversized detail request

A detail request for 245–250 s returns the two-second reminder.

If the question is “Which filter is selected?”, use overview on 245–250 s to locate a useful screen, then detail on a local range such as 245.2–246.2 with a crop. If the question is “How did the filter change?”, use motion on the relevant sequence. Once the question is answered, move on.

## Compare distant results

The user asks how the displayed total differs between 30 s and 90 s. Use separate detail reads near each location, for example 29.8–30.2 and 89.8–90.2, with comparable crops. Reuse an existing picture when it already shows one of the totals. Report the two values and their difference. Inspect the intervening process with motion only if the user also asks how the change happened.

## Finish a transcript explanation

A lecture transcript page ends with “We rejected the second design because …”. Copy the complete next call to read the reason, even if `processed_range` already reaches the requested endpoint. Once the explanation is clear, answer the targeted question or continue to the next topic for a whole-lecture summary.

Use the same decision for frame pages: continue when the remaining pictures are needed, start a fresh range for a different passage, and answer when the requested evidence is in hand.

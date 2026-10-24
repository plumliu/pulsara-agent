"""One real visible BrowserWindow closes through original main callbacks."""

import windows_probe as P

result = P.run_case("window-close", False, True, owner="browser-owner.cjs")
assert result.get("observed"), result.get("failure")
events = result["hostRows"]
visible = next(
    row for row in events if row.get("event") == "actual-browser-window-visible"
)
closed = next(
    row for row in events if row.get("event") == "actual-browser-window-close-event"
)
stop = next(row for row in events if row.get("event") == "before-original-pg-stop")
assert visible["visible"] and visible["time"] <= closed["time"] <= stop["time"]
assert not stop["state"]["kernel"]["alive"] and stop["state"]["lock"]["acquired"]
assert result["hostResult"]["appExit"] == 0

"""Observable playback behavior in the production double-sandbox document boundary."""

from collections.abc import Iterator
from pathlib import Path
import time

from playwright.sync_api import Frame, Page, sync_playwright
import pytest


_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def page() -> Iterator[Page]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1200, "height": 800})
            page.set_content("<!doctype html><main></main>")
            page.evaluate("""() => {
                window.playbackAcks = {};
                addEventListener('message', event => {
                    if (event.data?.type === 'playback-applied')
                        playbackAcks[event.data.name] = event.data.visible;
                });
            }""")
            yield page
        finally:
            browser.close()


def _mount(page: Page, html: str, *, name: str = "chart", visible: bool = True) -> Frame:
    kernel = _ROOT / "src/pulsara_agent/conversation_kernel"
    playback = (kernel / "visualization_playback.js").read_text().replace("export default ", "", 1)
    shell = (kernel / "visualization_shell.js").read_text().replace("export default ", "", 1)
    page.evaluate(r"""({html, playback, shell, name, visible}) => {
        const build = (0, eval)('(' + shell + ')');
        const bootstrap = '<script>' + playback + '\n' +
            'const setVisible = installVisualizationPlayback(' + visible + ');' +
            `addEventListener('message', event => {
                if (event.source !== parent || event.data?.type !== 'test-playback') return;
                setVisible(event.data.visible);
                parent.postMessage({type:'playback-applied', visible:event.data.visible}, '*');
            });` + '</script>';
        const setup = `addEventListener('message', event => {
            if (event.source === parent && event.data?.type === 'test-playback')
                frame.contentWindow.postMessage(event.data, '*');
            if (event.source === frame.contentWindow && event.data?.type === 'playback-applied')
                parent.postMessage({...event.data, name:${JSON.stringify(name)}}, '*');
        });`;
        const outer = document.createElement('iframe');
        outer.id = name;
        outer.setAttribute('sandbox', 'allow-scripts');
        outer.style.cssText = 'width:500px;height:300px;border:0';
        outer.srcdoc = build({html, bootstrap, setup});
        document.querySelector('main').append(outer);
    }""", {"html": html, "playback": playback, "shell": shell, "name": name, "visible": visible})
    outer = page.locator(f"#{name}").element_handle().content_frame()
    assert outer is not None
    outer.wait_for_selector("#content", state="attached")
    inner = outer.locator("#content").element_handle().content_frame()
    assert inner is not None
    inner.wait_for_load_state()
    assert inner.parent_frame is outer
    assert outer.parent_frame is page.main_frame
    return inner


def _visible(page: Page, visible: bool, *, name: str = "chart") -> None:
    page.evaluate("""({name, visible}) => {
        delete playbackAcks[name];
        document.getElementById(name).contentWindow.postMessage({type:'test-playback', visible}, '*');
    }""", {"name": name, "visible": visible})
    page.wait_for_function("({name, visible}) => playbackAcks[name] === visible", arg={"name": name, "visible": visible})


def _wait(page: Page, frame: Frame, expression: str) -> None:
    # Poll from the host: Playwright's in-frame polling evaluates source strings
    # from a later timer/RAF callback, which the production CSP correctly blocks.
    deadline = time.monotonic() + 5
    while not frame.evaluate(expression):
        assert time.monotonic() < deadline, expression
        page.wait_for_timeout(20)


def test_raf_pauses_without_losing_dom_and_leaves_other_frames_running(page: Page) -> None:
    html = """<input value="original"><div id="scroll" style="height:60px;overflow:auto"><div style="height:900px">content</div></div>
        <script>window.count=0;window.times=[];
        function tick(t){count++;times.push(t);requestAnimationFrame(tick)}requestAnimationFrame(tick);</script>"""
    frame = _mount(page, html)
    other = _mount(page, html, name="other")
    page.evaluate("() => {window.hostCount=0;function tick(){hostCount++;requestAnimationFrame(tick)}requestAnimationFrame(tick)}")
    frame.locator("input").fill("edited by user")
    frame.locator("#scroll").evaluate("el => el.scrollTop = 240")
    frame.evaluate("() => {window.originalInput=document.querySelector('input');window.model={chosen:17}}")
    _wait(page, frame, "count >= 4")
    _visible(page, False)
    before = frame.evaluate("({count, now:performance.now(), date:Date.now(), time:times.at(-1)})")
    other_before = other.evaluate("count")
    host_before = page.evaluate("hostCount")
    page.wait_for_timeout(250)
    stopped = frame.evaluate("({count, now:performance.now(), date:Date.now()})")
    assert stopped["count"] == before["count"]
    assert abs(stopped["now"] - before["now"]) < 1
    assert stopped["date"] - before["date"] >= 200
    assert other.evaluate("count") > other_before + 3
    assert page.evaluate("hostCount") > host_before + 3
    _visible(page, True)
    page.wait_for_timeout(100)
    after = frame.evaluate("({count, times, now:performance.now(), same:originalInput===document.querySelector('input'), chosen:model.chosen})")
    assert after["count"] > before["count"]
    assert 0 <= after["times"][before["count"]] - before["time"] < 150
    assert 30 <= after["now"] - before["now"] < 200
    assert after["same"] and after["chosen"] == 17
    assert frame.locator("input").input_value() == "edited by user"
    assert frame.locator("#scroll").evaluate("el => el.scrollTop") == 240


def test_raf_preserves_cancellation_exceptions_and_shared_frame_timestamp(page: Page) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    frame = _mount(page, "<p>animation scheduling</p>")
    frame.evaluate("""() => {
        window.calls=[];
        const cancelled=requestAnimationFrame(()=>calls.push('cancelled'));
        cancelAnimationFrame(cancelled);
        let later;
        requestAnimationFrame(t=>{calls.push(['first',t,performance.now()]);cancelAnimationFrame(later);
            requestAnimationFrame(next=>calls.push(['recursive',next,performance.now()]));});
        later=requestAnimationFrame(()=>calls.push('cancelled during batch'));
        requestAnimationFrame(()=>{throw new Error('expected RAF callback failure')});
        requestAnimationFrame(t=>calls.push(['last',t,performance.now()]));
    }""")
    _wait(page, frame, "calls.some(value => value[0] === 'recursive')")
    calls = frame.evaluate("calls")
    assert [value[0] for value in calls] == ["first", "last", "recursive"]
    assert calls[0][1] == calls[1][1]
    assert calls[2][1] > calls[0][1]
    assert all(0 <= value[2] - value[1] < 100 for value in calls)
    assert any("expected RAF callback failure" in error for error in errors)
    _visible(page, False)
    frame.evaluate("""() => {
        window.hiddenCalls=[];
        const cancelled=requestAnimationFrame(()=>hiddenCalls.push('cancelled'));
        cancelAnimationFrame(cancelled);
        requestAnimationFrame(t=>hiddenCalls.push([t,performance.now()]));
    }""")
    page.wait_for_timeout(120)
    assert frame.evaluate("hiddenCalls") == []
    _visible(page, True)
    _wait(page, frame, "hiddenCalls.length === 1")
    result = frame.evaluate("hiddenCalls[0]")
    assert 0 <= result[1] - result[0] < 100


def test_timers_keep_remaining_delay_arguments_receiver_and_cross_clear(page: Page) -> None:
    frame = _mount(page, "<p>timer scheduling</p>")
    frame.evaluate("""() => {
        window.fired=[];window.pulses=[];window.started=performance.now();
        setTimeout(function(a,b){fired.push({elapsed:performance.now()-started,receiver:this===window,a,b})},400,'argument',17);
        window.interval=setInterval(()=>pulses.push(performance.now()),100);
        const timeout=setTimeout(()=>fired.push('wrong timeout'),200);clearInterval(timeout);
        const cancelledInterval=setInterval(()=>fired.push('wrong interval'),20);clearTimeout(cancelledInterval);
    }""")
    page.wait_for_timeout(150)
    _visible(page, False)
    at_pause = frame.evaluate("({elapsed:performance.now()-started,pulses:pulses.length})")
    assert at_pause["elapsed"] < 350
    page.wait_for_timeout(450)
    assert frame.evaluate("fired") == []
    assert frame.evaluate("pulses.length") == at_pause["pulses"]
    _visible(page, True)
    page.wait_for_timeout(60)
    assert frame.evaluate("fired") == []
    assert frame.evaluate("pulses.length") <= at_pause["pulses"] + 1
    _wait(page, frame, "fired.length > 0")
    fired = frame.evaluate("fired")
    assert len(fired) == 1
    assert fired[0]["receiver"] is True
    assert (fired[0]["a"], fired[0]["b"]) == ("argument", 17)
    assert 380 <= fired[0]["elapsed"] < 600
    frame.evaluate("clearTimeout(interval)")
    pulses = frame.evaluate("pulses")
    assert len(pulses) <= 5
    assert all(right-left >= 70 for left, right in zip(pulses, pulses[1:]))
    page.wait_for_timeout(150)
    assert frame.evaluate("pulses") == pulses


def test_css_and_waapi_resume_phase_without_reviving_paused_or_cancelled_animation(page: Page) -> None:
    frame = _mount(page, """<style>@keyframes move{from{transform:translateX(0)}to{transform:translateX(200px)}}
        #css{animation:move 4s linear infinite}div{width:10px;height:10px}</style>
        <div id="css"></div><div id="running"></div><div id="paused"></div><div id="cancelled"></div>
        <script>
        window.run=document.querySelector('#running').animate([{opacity:0},{opacity:1}],{duration:4000,iterations:Infinity});
        window.held=document.querySelector('#paused').animate([{opacity:0},{opacity:1}],4000);held.pause();held.currentTime=600;
        window.gone=document.querySelector('#cancelled').animate([{opacity:0},{opacity:1}],4000);
        </script>""")
    page.wait_for_timeout(120)
    _visible(page, False)
    page.wait_for_timeout(40)
    before = frame.evaluate("({css:document.querySelector('#css').getAnimations()[0].currentTime,run:run.currentTime,held:held.currentTime})")
    frame.evaluate("""() => {
        gone.cancel();
        const target=document.createElement('div');document.body.append(target);
        window.added=target.animate([{opacity:0},{opacity:1}],{duration:4000,iterations:Infinity});
    }""")
    page.wait_for_timeout(40)
    new_before = frame.evaluate("added.currentTime")
    page.wait_for_timeout(250)
    after = frame.evaluate("({css:document.querySelector('#css').getAnimations()[0].currentTime,run:run.currentTime,held:held.currentTime,added:added.currentTime})")
    assert after["css"] == pytest.approx(before["css"], abs=2)
    assert after["run"] == pytest.approx(before["run"], abs=2)
    assert after["held"] == 600
    assert after["added"] == pytest.approx(new_before, abs=2)
    _visible(page, True)
    page.wait_for_timeout(120)
    resumed = frame.evaluate("({css:document.querySelector('#css').getAnimations()[0].currentTime,run:run.currentTime,held:held.playState,gone:gone.playState,added:added.currentTime})")
    assert 40 < resumed["css"] - after["css"] < 220
    assert 40 < resumed["run"] - after["run"] < 220
    assert resumed["held"] == "paused"
    assert resumed["gone"] == "idle"
    assert 40 < resumed["added"] - after["added"] < 220


def test_smil_pauses_and_an_initially_hidden_document_does_not_advance(page: Page) -> None:
    frame = _mount(page, """<svg id="moving" width="200" height="30"><rect width="10" height="10">
        <animate attributeName="x" from="0" to="100" dur="4s" repeatCount="indefinite"/></rect></svg>
        <svg id="held" width="200" height="30"><rect width="10" height="10">
        <animate attributeName="x" from="0" to="100" dur="4s" repeatCount="indefinite"/></rect></svg>
        <script>window.rafCount=0;window.timers=0;
        function tick(){window.rafCount++;requestAnimationFrame(tick)}requestAnimationFrame(tick);
        setTimeout(()=>timers++,100);document.querySelector('#held').pauseAnimations();</script>""", visible=False)
    page.wait_for_timeout(60)
    start = frame.evaluate("({frames:rafCount,timers,moving:document.querySelector('#moving').getCurrentTime()})")
    page.wait_for_timeout(200)
    assert frame.evaluate("rafCount") == 0
    assert frame.evaluate("timers") == 0
    assert frame.evaluate("document.querySelector('#moving').getCurrentTime()") == pytest.approx(start["moving"], abs=.005)
    _visible(page, True)
    page.wait_for_timeout(160)
    assert frame.evaluate("rafCount") >= 3
    assert frame.evaluate("timers") == 1
    assert frame.evaluate("document.querySelector('#held').animationsPaused()") is True
    _visible(page, False)
    time = frame.evaluate("document.querySelector('#moving').getCurrentTime()")
    page.wait_for_timeout(150)
    assert frame.evaluate("document.querySelector('#moving').getCurrentTime()") == pytest.approx(time, abs=.005)
    _visible(page, True)
    page.wait_for_timeout(100)
    resumed = frame.evaluate("document.querySelector('#moving').getCurrentTime()")
    assert .04 < resumed - time < .2


def test_string_timers_remain_blocked_by_csp_and_callback_errors_are_isolated(page: Page) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    frame = _mount(page, """<script>
        window.stringRan=false;window.callbacks=[];window.intervalCalls=0;
        setTimeout('window.stringRan=true',0);
        setInterval('window.stringRan=true',1);
        setTimeout(()=>{callbacks.push('throws');throw new Error('expected timer failure')},0);
        setTimeout(()=>callbacks.push('continues'),0);
        const interval=setInterval(()=>{
            intervalCalls++;
            if(intervalCalls===3)clearTimeout(interval);
            throw new Error('expected interval failure');
        },20);
        </script>""", visible=False)
    page.wait_for_timeout(80)
    assert frame.evaluate("stringRan") is False
    assert frame.evaluate("callbacks") == []
    assert frame.evaluate("intervalCalls") == 0
    _visible(page, True)
    _wait(page, frame, "intervalCalls === 3")
    assert frame.evaluate("callbacks") == ["throws", "continues"]
    assert frame.evaluate("stringRan") is False
    assert any("expected timer failure" in error for error in errors)
    assert sum("expected interval failure" in error for error in errors) == 3


def test_dynamic_css_transition_and_author_pause_during_hidden_state(page: Page) -> None:
    frame = _mount(page, """<style>
        @keyframes grow{from{width:10px}to{width:210px}}
        #transition{width:10px;transition:width 4s linear}
        .animated{animation:grow 4s linear infinite}div{height:10px}
        </style><div id="transition"></div><div id="author" class="animated"></div>""")
    frame.locator("#transition").evaluate("el=>{getComputedStyle(el).width;el.style.width='210px'}")
    page.wait_for_timeout(100)
    _visible(page, False)
    frame.evaluate("""() => {
        document.querySelector('#author').style.animationPlayState='paused';
        const element=document.createElement('div');element.id='newcss';element.className='animated';
        document.body.append(element);
    }""")
    page.wait_for_timeout(50)
    snapshot = """() => Object.fromEntries(['transition','author','newcss'].map(id=>
        [id,{width:parseFloat(getComputedStyle(document.getElementById(id)).width),
        state:document.getElementById(id).getAnimations()[0].playState}]))"""
    before = frame.evaluate(snapshot)
    page.wait_for_timeout(200)
    after = frame.evaluate(snapshot)
    for name in before:
        assert after[name]["width"] == pytest.approx(before[name]["width"], abs=.1)
    _visible(page, True)
    page.wait_for_timeout(120)
    resumed = frame.evaluate(snapshot)
    for name in ["transition", "newcss"]:
        assert 2 < resumed[name]["width"] - after[name]["width"] < 12
    assert resumed["author"]["width"] == pytest.approx(after["author"]["width"], abs=.1)
    assert resumed["author"]["state"] == "paused"


def test_environment_bridge_controls_playback_without_content_subscription(page: Page) -> None:
    environment = (_ROOT / "src/pulsara_agent/conversation_kernel/visualization_environment.js").read_text().replace("export default ", "", 1)
    frame = _mount(page, "<script>" + environment + """
        const environment={theme:'light',visible:true,reducedMotion:false,colors:{
            background:'#fff',foreground:'#111',muted:'#555',accent:'#123',border:'#ddd',soft:'#eee'}};
        installVisualizationEnvironment(environment,setVisible);
        window.count=0;function tick(){count++;requestAnimationFrame(tick)}requestAnimationFrame(tick);
        </script>""")
    _wait(page, frame, "count >= 3")
    # The real owner is the outer document. A self-sent forged environment
    # message must not acquire that owner's authority.
    frame.evaluate("postMessage({type:'pulsara-visualization-environment',environment:{...PulsaraVisualization.getSnapshot(),visible:false}},'*')")
    page.wait_for_timeout(30)
    assert frame.evaluate("PulsaraVisualization.getSnapshot().visible") is True
    state = frame.evaluate("PulsaraVisualization.getSnapshot()")
    state["visible"] = False
    outer = frame.parent_frame
    assert outer is not None
    outer.evaluate("environment=>document.querySelector('#content').contentWindow.postMessage({type:'pulsara-visualization-environment',environment},'*')", state)
    _wait(page, frame, "PulsaraVisualization.getSnapshot().visible === false")
    count = frame.evaluate("count")
    page.wait_for_timeout(120)
    assert frame.evaluate("count") == count
    state["visible"] = True
    outer.evaluate("environment=>document.querySelector('#content').contentWindow.postMessage({type:'pulsara-visualization-environment',environment},'*')", state)
    _wait(page, frame, f"count > {count}")


def test_css_author_pause_and_running_still_work_after_host_resume(page: Page) -> None:
    frame = _mount(page, """<style>@keyframes slide{from{transform:translateX(0)}to{transform:translateX(200px)}}
        #box{width:10px;height:10px;animation:slide 4s linear infinite}</style><div id="box"></div>""")
    page.wait_for_timeout(80)
    _visible(page, False)
    page.wait_for_timeout(100)
    _visible(page, True)
    page.wait_for_timeout(80)
    frame.locator("#box").evaluate("el=>el.style.animationPlayState='paused'")
    page.wait_for_timeout(40)
    stopped = frame.evaluate("document.querySelector('#box').getAnimations()[0].currentTime")
    assert frame.evaluate("document.querySelector('#box').getAnimations()[0].playState") == "paused"
    page.wait_for_timeout(160)
    assert frame.evaluate("document.querySelector('#box').getAnimations()[0].currentTime") == pytest.approx(stopped, abs=2)
    frame.locator("#box").evaluate("el=>el.style.animationPlayState='running'")
    page.wait_for_timeout(120)
    resumed = frame.evaluate("document.querySelector('#box').getAnimations()[0].currentTime")
    assert 40 < resumed - stopped < 220
    assert frame.evaluate("document.querySelector('#box').getAnimations()[0].playState") == "running"


def test_css_hidden_rate_reverse_and_author_pause_intents_survive_resume(page: Page) -> None:
    frame = _mount(page, """<style>@keyframes slide{from{transform:translateX(0)}to{transform:translateX(200px)}}
        div{width:10px;height:10px;animation:slide 4s linear infinite}</style>
        <div id="direct"></div><div id="updated"></div><div id="reversed"></div>
        <div id="csspaused"></div><div id="apipaused"></div>""")
    frame.evaluate("""() => {
        window.animations=Object.fromEntries(Array.from(document.querySelectorAll('div'),el=>[el.id,el.getAnimations()[0]]));
        for(const animation of Object.values(animations))animation.currentTime=1500;
    }""")
    page.wait_for_timeout(60)
    _visible(page, False)
    frame.evaluate("""() => {
        animations.direct.playbackRate=2;
        animations.updated.updatePlaybackRate(3);
        animations.reversed.reverse();
        document.querySelector('#csspaused').style.animationPlayState='paused';
        animations.apipaused.pause();
    }""")
    page.wait_for_timeout(40)
    snapshot = "() => Object.fromEntries(Object.entries(animations).map(([name,a])=>[name,{time:a.currentTime,rate:a.playbackRate,state:a.playState}]))"
    before = frame.evaluate(snapshot)
    page.wait_for_timeout(160)
    after = frame.evaluate(snapshot)
    for name in before:
        assert after[name]["time"] == pytest.approx(before[name]["time"], abs=2)
    _visible(page, True)
    page.wait_for_timeout(120)
    resumed = frame.evaluate(snapshot)
    for name, rate in [("direct", 2), ("updated", 3), ("reversed", -1)]:
        assert resumed[name]["rate"] == rate
        elapsed = (resumed[name]["time"] - after[name]["time"]) / rate
        assert 40 < elapsed < 220
    for name in ["csspaused", "apipaused"]:
        assert resumed[name]["state"] == "paused"
        assert resumed[name]["time"] == pytest.approx(after[name]["time"], abs=2)
        assert resumed[name]["rate"] == 1
    frame.evaluate("""() => {
        document.querySelector('#csspaused').style.animationPlayState='running';
        animations.apipaused.play();
    }""")
    page.wait_for_timeout(120)
    author_resumed = frame.evaluate(snapshot)
    for name in ["csspaused", "apipaused"]:
        assert 40 < author_resumed[name]["time"] - resumed[name]["time"] < 220


def test_hidden_detach_and_reattach_does_not_strand_animation_or_smil_paused(page: Page) -> None:
    frame = _mount(page, """<div id="target"></div>
        <svg id="svg" width="200" height="30"><rect width="10" height="10">
        <animate attributeName="x" from="0" to="100" dur="4s" repeatCount="indefinite"/></rect></svg>
        <script>window.target=document.querySelector('#target');window.svg=document.querySelector('#svg');
        window.animation=target.animate([{opacity:0},{opacity:1}],{duration:4000,iterations:Infinity});</script>""")
    page.wait_for_timeout(80)
    _visible(page, False)
    frame.evaluate("() => {target.remove();svg.remove()}")
    # Let the hidden-document observer process removal before reattaching the
    # same objects; synchronous remove/append would conceal lost ownership.
    page.wait_for_timeout(60)
    frame.evaluate("() => {document.body.append(target,svg)}")
    page.wait_for_timeout(40)
    before = frame.evaluate("({animation:animation.currentTime,svg:svg.getCurrentTime()})")
    page.wait_for_timeout(100)
    after = frame.evaluate("({animation:animation.currentTime,svg:svg.getCurrentTime()})")
    assert after["animation"] == pytest.approx(before["animation"], abs=2)
    assert after["svg"] == pytest.approx(before["svg"], abs=.005)
    _visible(page, True)
    page.wait_for_timeout(120)
    resumed = frame.evaluate("({animation:animation.currentTime,svg:svg.getCurrentTime()})")
    assert 40 < resumed["animation"] - after["animation"] < 220
    assert .04 < resumed["svg"] - after["svg"] < .22


def test_actual_offscreen_frame_holds_all_animation_phases_and_resumes_without_jump(page: Page) -> None:
    frame = _mount(page, """<style>
        @keyframes move{from{transform:translateX(0)}to{transform:translateX(200px)}}
        #css{animation:move 4s linear infinite}div{width:10px;height:10px}
        #transition{transition:width 4s linear}
        </style><div id="css"></div><div id="waapi"></div><div id="transition"></div>
        <svg id="svg" width="200" height="30"><rect width="10" height="10">
        <animate attributeName="x" from="0" to="100" dur="4s" repeatCount="indefinite"/></rect></svg>
        <script>window.waapi=document.querySelector('#waapi').animate([{opacity:0},{opacity:1}],{duration:4000,iterations:Infinity});</script>""")
    frame.locator("#transition").evaluate("el=>{getComputedStyle(el).width;el.style.width='210px'}")
    page.wait_for_timeout(100)
    frame.evaluate("window.transition=document.querySelector('#transition').getAnimations()[0]")
    page.evaluate("""() => {
        const spacer=document.createElement('div');spacer.style.height='2000px';document.body.append(spacer);
        window.chartIntersects=true;
        const observer=new IntersectionObserver(entries=>{chartIntersects=entries[0].isIntersecting});
        observer.observe(document.querySelector('#chart'));
        scrollTo(0,900);
    }""")
    page.wait_for_function("chartIntersects === false")
    assert page.locator("#chart").evaluate("el=>el.getBoundingClientRect().bottom < 0")
    _visible(page, False)
    snapshot = """() => ({
        css:document.querySelector('#css').getAnimations()[0].currentTime,
        waapi:waapi.currentTime,transition:transition.currentTime,
        smil:document.querySelector('#svg').getCurrentTime()*1000
    })"""
    before = frame.evaluate(snapshot)
    page.wait_for_timeout(250)
    stopped = frame.evaluate(snapshot)
    assert stopped == pytest.approx(before, abs=2)
    # Returning the native frame to the viewport must not charge the time spent
    # offscreen to its animation, even if no native RAF ran to settle a pause.
    page.evaluate("scrollTo(0,0)")
    page.wait_for_function("chartIntersects === true")
    _visible(page, True)
    page.wait_for_timeout(120)
    resumed = frame.evaluate(snapshot)
    for kind in before:
        assert 40 < resumed[kind] - stopped[kind] < 220, kind

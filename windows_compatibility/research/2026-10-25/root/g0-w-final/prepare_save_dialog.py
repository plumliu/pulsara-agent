"""Reuse the executed HTTP fixture; only replace its selected Windows cases."""

from pathlib import Path

out = Path(__file__).parent
source = (out.parent / "round5/http_host.cjs").read_text(encoding="utf-8")
prefix = source[: source.index(" await test('actual middleware")]
suffix = source[source.index("app.whenReady().then(main)") :]
body = r"""
 const target=path.join(out,'save-dialog-existing.txt');
 const newTarget=path.join(out,'save-dialog-cancelled.txt');
 assert.ok(!fs.existsSync(target)&&!fs.existsSync(newTarget),'fresh output filenames');
 fs.writeFileSync(target,'G0-owned overwrite sentinel');
 const item=await preview('中文.txt');
 win.setTitle('G0 Windows 保存对话框验收');
 await win.webContents.executeJavaScript("document.body.textContent='G0 保存对话框：只操作本次 output 中的合成测试文件。'");
 for (const stage of ['cancel-dialog','decline-overwrite','accept-overwrite']) {
  report.stage=stage;save();
  const result=await new Promise(resolve=>{
   ses.once('will-download',(_e,download)=>{
    download.setSaveDialogOptions({title:'G0 '+stage,defaultPath:stage==='cancel-dialog'?newTarget:target,properties:['showOverwriteConfirmation']});
    report.dialogOptions=download.getSaveDialogOptions();save();
    download.once('done',(_e,state)=>resolve({state,path:download.getSavePath(),bytes:download.getReceivedBytes()}));
   });
   win.webContents.downloadURL(ready.origin+item.content_url+'?download=1');
  });
  if(stage==='accept-overwrite') {
   assert.equal(result.state,'completed');
   assert.equal(fs.readFileSync(target,'utf8'),'中文预览 / Windows WSL2\n');
  } else {
   assert.equal(result.state,'cancelled');
   assert.equal(fs.readFileSync(target,'utf8'),'G0-owned overwrite sentinel');
   assert.ok(!fs.existsSync(newTarget));
  }
  report.tests.push({name:stage,pass:true,detail:result});save();
 }
 report.stage='complete';save();
}
"""
candidate = prefix + body + suffix
candidate = candidate.replace(
    "path.join(out,'http_guest.py')", "path.join(out,'../round5/http_guest.py')"
)
candidate = candidate.replace("http-", "save-dialog-")
# Keep the cross-round source name after replacing this probe's result/log prefixes.
candidate = candidate.replace(
    "../round5/save-dialog-guest.py", "../round5/http_guest.py"
)
(out / "save-dialog-host.cjs").write_text(candidate, encoding="utf-8")
print("Prepared native save dialog probe; not launched")

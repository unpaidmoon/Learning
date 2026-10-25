"""Real outer launcher recovery; deliberate death uses only owned Popen handles."""

import json
import os
import socket
import subprocess
import sys

import pytest

from learning_app.config import PROJECT_ROOT
from learning_app.processes import require_free_port
from learning_app.storage import Repository


@pytest.mark.parametrize("crashes,expected_code", [(1, 0), (3, 1)])
def test_outer_launcher_restarts_only_twice_and_keeps_original_business_data(
    tmp_path, crashes, expected_code
):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<p>owned fixture</p>", "utf-8")
    repo = Repository(tmp_path / "data")
    repo.create_course("恢复合成课程", "course")
    repo.create_chat("course", "chat")
    pending = repo.publish_submission("chat", "old question must not be replayed", "original")
    before = repo.get_generation(pending["generation_id"])
    repo.close()
    script = tmp_path / "run_fixture.py"
    script.write_text(
        """import json,os,threading,time
from pathlib import Path
from urllib.request import build_opener,ProxyHandler
import learning_app.dev as dev
from learning_app.processes import OwnedProcessGroup
created=[];errors=[];root=Path(os.environ['LEARNING_APP_RUNTIME_DIR']);crashes=int(os.environ['LEARNING_TEST_CRASHES'])
class Observed(OwnedProcessGroup):
 def spawn(self,args,**kw):
  child=super().spawn(args,**kw)
  if '-m' in args and 'learning_app' in args:
   identity=kw['env']['LEARNING_APP_INSTANCE_ID'];index=len(created)+1;created.append(identity)
   def fault():
    try:
     opener=build_opener(ProxyHandler({}));deadline=time.monotonic()+25
     while time.monotonic()<deadline and child.poll() is None:
      record_path=root/'apps'/identity/'run.json'
      try:
       record=json.loads(record_path.read_text('utf-8'))
       with opener.open('http://127.0.0.1:'+os.environ['LEARNING_TEST_PORT']+'/api/v1/health',timeout=.5) as response: health=json.load(response)
       if record['status']=='ready' and health['instance_id']==identity:break
      except Exception:pass
      time.sleep(.02)
     else:raise RuntimeError('owned backend not ready')
     if index<=crashes:child.kill()
     else:dev._request_owned_exit(record,confirm_active=True,timeout=5)
    except Exception as error:
     errors.append(type(error).__name__+':'+str(error))
     if child.poll() is None:child.kill()
   threading.Thread(target=fault,daemon=True).start()
  return child
dev.OwnedProcessGroup=Observed;dev.BACKEND_RECOVERY_DELAYS=(.01,.02)
code=dev.run(['start','--no-harness','--backend-port',os.environ['LEARNING_TEST_PORT'],'--timeout','20'])
Path(os.environ['LEARNING_TEST_RESULT']).write_text(json.dumps({'code':code,'created':created,'errors':errors}),'utf-8')
raise SystemExit(code)
""",
        "utf-8",
    )
    env = {
        **os.environ,
        "LEARNING_APP_DATA_DIR": str(tmp_path / "data"),
        "LEARNING_APP_RUNTIME_DIR": str(tmp_path / "run"),
        "LEARNING_APP_STATIC_DIR": str(static),
        "LEARNING_APP_HARNESS_ENABLED": "false",
        "LEARNING_TEST_CRASHES": str(crashes),
        "LEARNING_TEST_PORT": str(port),
        "LEARNING_TEST_RESULT": str(tmp_path / "result.json"),
    }
    hidden = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
        **hidden,
    )
    assert result.returncode == expected_code, result.stdout + result.stderr
    observed = json.loads((tmp_path / "result.json").read_text("utf-8"))
    assert observed["errors"] == []
    assert len(observed["created"]) == min(crashes + 1, 3)
    assert len(set(observed["created"])) == len(observed["created"])
    records = [
        json.loads((tmp_path / "run/apps" / identity / "run.json").read_text("utf-8"))
        for identity in observed["created"]
    ]
    assert all(row["cleanup"]["remaining_pids"] == [] for row in records)
    assert records[-1]["status"] == ("stopped" if expected_code == 0 else "failed")
    for previous, current in zip(records, records[1:]):
        assert current["recovery_parent"] == previous["instance_id"]
    require_free_port("127.0.0.1", port)
    restored = Repository(tmp_path / "data")
    after = restored.get_generation(pending["generation_id"])
    if expected_code == 0:
        assert after["cancel_requested"] and after["state"] == "cancelled"
        assert after["body"] == before["body"]
        assert after["submission_id"] == before["submission_id"]
        assert after["context_snapshot"] == before["context_snapshot"]
    else:
        assert after == before
    assert restored.counts()["outbound_attempts"] == 0
    restored.close()

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from gitdrip.config import Config, GitdripError

IS_WINDOWS = sys.platform == "win32"


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def _task_command(project: Path) -> tuple[str, str]:
    exe = shutil.which("gitdrip")
    if exe:
        return exe, f'run --scheduled --project "{project}"'
    return sys.executable, f'-m gitdrip run --scheduled --project "{project}"'


def _task_xml(cfg: Config, project: Path, time: str) -> str:
    program, arguments = _task_command(project)
    start = dt.datetime.now().replace(hour=int(time[:2]), minute=int(time[3:]), second=0, microsecond=0)
    if start <= dt.datetime.now():
        start += dt.timedelta(days=1)
    boundary = start.isoformat(timespec="seconds")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>gitdrip daily push for {escape(str(project))}</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{boundary}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(program)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(str(project))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""


def schedule(cfg: Config, project: Path, time: str | None = None) -> str:
    push_time = time or cfg.push_time
    if not IS_WINDOWS:
        cron = f"{push_time} * * * cd {project} && {sys.executable} -m gitdrip run --scheduled"
        return f"add this to crontab:\n  {cron}"
    xml = _task_xml(cfg, project, push_time)
    fd, xml_path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as fh:
            fh.write(xml)
        proc = _schtasks("/Create", "/F", "/TN", cfg.task_name, "/XML", xml_path)
    finally:
        try:
            os.unlink(xml_path)
        except OSError:
            pass
    if proc.returncode != 0:
        raise GitdripError(f"schtasks failed: {(proc.stderr or proc.stdout).strip()}")
    return f"scheduled task '{cfg.task_name}' daily at {push_time}"


def unschedule(cfg: Config) -> str:
    if not IS_WINDOWS:
        return f"remove the cron line for task '{cfg.task_name}' manually"
    proc = _schtasks("/Delete", "/F", "/TN", cfg.task_name)
    if proc.returncode != 0:
        out = (proc.stderr or proc.stdout).strip()
        if "cannot find" in out.lower() or "not found" in out.lower():
            return f"task '{cfg.task_name}' is not scheduled"
        raise GitdripError(f"schtasks failed: {out}")
    return f"removed task '{cfg.task_name}'"


def schedule_status(cfg: Config) -> str:
    if not IS_WINDOWS:
        return "scheduler status available on Windows only"
    proc = _schtasks("/Query", "/TN", cfg.task_name, "/FO", "LIST")
    if proc.returncode != 0:
        return "not scheduled"
    info = {}
    for line in proc.stdout.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            info[key.strip().lower()] = value.strip()
    return f"scheduled: {info.get('next run time', 'unknown next run')}"

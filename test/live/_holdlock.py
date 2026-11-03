"""辅助脚本：拿住 organize.lock 不放，供 test_crash_lock.py 强杀。"""

import sys
import time

root, core = sys.argv[1], sys.argv[2]
sys.path.insert(0, core)

from organizer import Organizer  # noqa: E402

org = Organizer(root, "astrbot", {})
if not org.acquire_lock():
    print("FAILED", flush=True)
    sys.exit(1)
print("LOCKED", flush=True)
time.sleep(120)

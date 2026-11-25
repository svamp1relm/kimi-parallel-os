from __future__ import annotations

import sys

from .orchestrator import Supervisor, default_home, run_paths, secure_home, write_json


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    run_id = sys.argv[1]
    try:
        home = secure_home(default_home())
        Supervisor(run_id, home).run()
    except Exception as error:
        try:
            write_json(run_paths(run_id, home).root / "startup-error.json", {"message": str(error)})
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env bash
# OpenFOAM_GUI entrypoint: install/refresh Python deps from the mounted repo, then serve.
set -euo pipefail

if [ ! -f /gui/main.py ]; then
    echo "[foamgui] /gui is empty — bind-mount the OpenFOAM_GUI repo (-v <repo>:/gui)" >&2
    exit 1
fi

echo "[foamgui] installing Python deps..."
pip install --quiet -r /gui/requirements.txt
for req in /gui/modules/*/requirements.txt; do
    [ -f "$req" ] && pip install --quiet -r "$req"
done

echo "[foamgui] starting on 0.0.0.0:6060 (OpenFOAM $(ls /usr/lib/openfoam/ 2>/dev/null | head -1))"
cd /gui
export PYTHONPATH=/gui # modules import the repo-root `shared` package
exec uvicorn main:app --host 0.0.0.0 --port 6060

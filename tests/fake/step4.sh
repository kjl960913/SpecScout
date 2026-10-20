#!/bin/bash
# scripted step 4: a trivial implementation, then one differential run
set -e
mkdir -p work/impl
cat > work/impl/main.py <<'PY'
import sys
sys.stdout.write("")
PY
printf '#!/bin/sh\nset -e\n{ echo "#!/usr/bin/env python3"; cat main.py; } > executable\nchmod +x executable\n' > work/impl/compile.sh
./pk diff | head -12

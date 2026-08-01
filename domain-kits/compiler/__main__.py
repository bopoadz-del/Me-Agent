"""Allow `python -m domain_kits.compiler` as well as `.engine`."""
from domain_kits.compiler.engine import main

raise SystemExit(main())

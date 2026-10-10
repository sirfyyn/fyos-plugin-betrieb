"""`python -m fyos_plugin_betrieb.graph_einspeisen` — wie das Konsolenskript `fyos-graph-einspeisen`."""

import sys

from .einspeisen import main

sys.exit(main())

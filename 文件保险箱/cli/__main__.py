"""允许 python -m cli 运行。"""

import sys

from .main import main

sys.exit(main())

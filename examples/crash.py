"""An uncaught exception: reported as an unhandled error, sent, then printed as usual.

    ZHARP_KEY=zpk_… python crash.py
"""

import zharp

zharp.init(service="python-example", environment="development")
zharp.log.info("About to crash")
raise SystemError("Uncaught at the top level")

#**************************************************************************
# * Copyright (C) 2023-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the hybridIO project.
# *
# * HybridIO can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
import time
from flexEngine.supervisor.supervisor import supervisor

if __name__ == '__main__':
    agent = supervisor(config="configuration/config.yaml",verbose=False)
    agent.start()
    try:
       while True:
           time.sleep(1)
    except:
       agent.stop()
       exit()
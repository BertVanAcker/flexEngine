# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
import redis

# Connect to Redis
r = redis.Redis(host='localhost', port=6379, db=4)

# Flush only DB 7
r.flushdb()
print("Demonstrator database (DB 7) cleaned.")

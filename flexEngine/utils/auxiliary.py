#**************************************************************************
# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
from threading import Timer
import yaml
import configparser

class perpetualTimer():

   def __init__(self,t,hFunction):
      self.t=t
      self.hFunction = hFunction
      #self.thread = Timer(self.t,self.handle_function)
      self.thread = Timer(self.t, self.handle_function)
      x=1

   def handle_function(self):
      self.hFunction()
      self.thread = Timer(self.t,self.handle_function)
      self.thread.start()

   def start(self):
      self.thread.start()

   def cancel(self):
      self.thread.cancel()

   def isAlive(self):
      return self.thread.is_alive()


def load_config(config_file):
  with open(config_file, 'r') as file:
      return yaml.safe_load(file)


def load_flexia_config(config_file):
   # read the configuration file
   config = configparser.ConfigParser()
   config.optionxform = str  # preserve case of keys
   config.read(config_file)

   data = {}

   # Process all sections
   for section in config.sections():
      if ":" in section:
         # Split sections like 'build:Oberon-5' into prefix and variable name
         prefix, variable = section.split(":", 1)
         if prefix not in data:
            data[prefix] = {}
         data[prefix][variable] = dict(config[section])
      else:
         data[section] = dict(config[section])

   return data

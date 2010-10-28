"""
Helper functions for working with tor's configuration file.
"""

import curses
import threading

from util import sysTools, torTools, uiTools

CONFIG = {"features.torrc.validate": True,
          "torrc.multiline": [],
          "torrc.alias": {},
          "torrc.label.size.b": [],
          "torrc.label.size.kb": [],
          "torrc.label.size.mb": [],
          "torrc.label.size.gb": [],
          "torrc.label.size.tb": [],
          "torrc.label.time.sec": [],
          "torrc.label.time.min": [],
          "torrc.label.time.hour": [],
          "torrc.label.time.day": [],
          "torrc.label.time.week": []}

# enums and values for numeric torrc entries
UNRECOGNIZED, SIZE_VALUE, TIME_VALUE = range(1, 4)
SIZE_MULT = {"b": 1, "kb": 1024, "mb": 1048576, "gb": 1073741824, "tb": 1099511627776}
TIME_MULT = {"sec": 1, "min": 60, "hour": 3600, "day": 86400, "week": 604800}

# enums for issues found during torrc validation:
# VAL_DUPLICATE - entry is ignored due to being a duplicate
# VAL_MISMATCH  - the value doesn't match tor's current state
VAL_DUPLICATE, VAL_MISMATCH = range(1, 3)

TORRC = None # singleton torrc instance

def loadConfig(config):
  CONFIG["torrc.multiline"] = config.get("torrc.multiline", [])
  CONFIG["torrc.alias"] = config.get("torrc.alias", {})
  
  # all the torrc.label.* values are comma separated lists
  for configKey in CONFIG.keys():
    if configKey.startswith("torrc.label."):
      configValues = config.get(configKey, "").split(",")
      if configValues: CONFIG[configKey] = [val.strip() for val in configValues]

def getTorrc():
  """
  Singleton constructor for a Controller. Be aware that this starts as being
  unloaded, needing the torrc contents to be loaded before being functional.
  """
  
  global TORRC
  if TORRC == None: TORRC = Torrc()
  return TORRC

def getConfigLocation():
  """
  Provides the location of the torrc, raising an IOError with the reason if the
  path can't be determined.
  """
  
  conn = torTools.getConn()
  configLocation = conn.getInfo("config-file")
  if not configLocation: raise IOError("unable to query the torrc location")
  
  # checks if this is a relative path, needing the tor pwd to be appended
  if configLocation[0] != "/":
    torPid = conn.getMyPid()
    failureMsg = "querying tor's pwd failed because %s"
    if not torPid: raise IOError(failureMsg % "we couldn't get the pid")
    
    try:
      # pwdx results are of the form:
      # 3799: /home/atagar
      # 5839: No such process
      results = sysTools.call("pwdx %s" % torPid)
      if not results:
        raise IOError(failureMsg % "pwdx didn't return any results")
      elif results[0].endswith("No such process"):
        raise IOError(failureMsg % ("pwdx reported no process for pid " + torPid))
      elif len(results) != 1 or results.count(" ") != 1:
        raise IOError(failureMsg % "we got unexpected output from pwdx")
      else:
        pwdPath = results[0][results[0].find(" ") + 1:]
        configLocation = "%s/%s" % (pwdPath, configLocation)
    except IOError, exc:
      raise IOError(failureMsg % ("the pwdx call failed: " + str(exc)))
  
  return torTools.getPathPrefix() + configLocation

def validate(contents = None):
  """
  Performs validation on the given torrc contents, providing back a mapping of
  line numbers to tuples of the (issue, msg) found on them.
  
  Arguments:
    contents - torrc contents
  """
  
  conn = torTools.getConn()
  contents = _stripComments(contents)
  issuesFound, seenOptions = {}, []
  for lineNumber in range(len(contents) - 1, -1, -1):
    lineText = contents[lineNumber]
    if not lineText: continue
    
    lineComp = lineText.split(None, 1)
    if len(lineComp) == 2: option, value = lineComp
    else: option, value = lineText, ""
    
    # most parameters are overwritten if defined multiple times
    if option in seenOptions and not option in CONFIG["torrc.multiline"]:
      issuesFound[lineNumber] = (VAL_DUPLICATE, "")
      continue
    else: seenOptions.append(option)
    
    # replace aliases with their recognized representation
    if option in CONFIG["torrc.alias"]:
      option = CONFIG["torrc.alias"][option]
    
    # tor appears to replace tabs with a space, for instance:
    # "accept\t*:563" is read back as "accept *:563"
    value = value.replace("\t", " ")
    
    # parse value if it's a size or time, expanding the units
    value, valueType = _parseConfValue(value)
    
    # issues GETCONF to get the values tor's currently configured to use
    torValues = conn.getOption(option, [], True)
    
    # multiline entries can be comma separated values (for both tor and conf)
    valueList = [value]
    if option in CONFIG["torrc.multiline"]:
      valueList = [val.strip() for val in value.split(",")]
      
      fetchedValues, torValues = torValues, []
      for fetchedValue in fetchedValues:
        for fetchedEntry in fetchedValue.split(","):
          fetchedEntry = fetchedEntry.strip()
          if not fetchedEntry in torValues:
            torValues.append(fetchedEntry)
    
    for val in valueList:
      # checks if both the argument and tor's value are empty
      isBlankMatch = not val and not torValues
      
      if not isBlankMatch and not val in torValues:
        # converts corrections to reader friedly size values
        displayValues = torValues
        if valueType == SIZE_VALUE:
          displayValues = [uiTools.getSizeLabel(int(val)) for val in torValues]
        elif valueType == TIME_VALUE:
          displayValues = [uiTools.getTimeLabel(int(val)) for val in torValues]
        
        issuesFound[lineNumber] = (VAL_MISMATCH, ", ".join(displayValues))
  
  return issuesFound

def _parseConfValue(confArg):
  """
  Converts size or time values to their lowest units (bytes or seconds) which
  is what GETCONF calls provide. The returned is a tuple of the value and unit
  type.
  
  Arguments:
    confArg - torrc argument
  """
  
  if confArg.count(" ") == 1:
    val, unit = confArg.lower().split(" ", 1)
    if not val.isdigit(): return confArg, UNRECOGNIZED
    mult, multType = _getUnitType(unit)
    
    if mult != None:
      return str(int(val) * mult), multType
  
  return confArg, UNRECOGNIZED

def _getUnitType(unit):
  """
  Provides the type and multiplier for an argument's unit. The multiplier is
  None if the unit isn't recognized.
  
  Arguments:
    unit - string representation of a unit
  """
  
  for label in SIZE_MULT:
    if unit in CONFIG["torrc.label.size." + label]:
      return SIZE_MULT[label], SIZE_VALUE
  
  for label in TIME_MULT:
    if unit in CONFIG["torrc.label.time." + label]:
      return TIME_MULT[label], TIME_VALUE
  
  return None, UNRECOGNIZED

def _stripComments(contents):
  """
  Removes comments and extra whitespace from the given torrc contents.
  
  Arguments:
    contents - torrc contents
  """
  
  strippedContents = []
  for line in contents:
    if line and "#" in line: line = line[:line.find("#")]
    strippedContents.append(line.strip())
  return strippedContents

class Torrc():
  """
  Wrapper for the torrc. All getters provide None if the contents are unloaded.
  """
  
  def __init__(self):
    self.contents = None
    self.configLocation = None
    self.valsLock = threading.RLock()
    
    # cached results for the current contents
    self.displayableContents = None
    self.strippedContents = None
    self.corrections = None
  
  def load(self):
    """
    Loads or reloads the torrc contents, raising an IOError if there's a
    problem.
    """
    
    self.valsLock.acquire()
    
    # clears contents and caches
    self.contents, self.configLocation = None, None
    self.displayableContents = None
    self.strippedContents = None
    self.corrections = None
    
    try:
      self.configLocation = getConfigLocation()
      configFile = open(self.configLocation, "r")
      self.contents = configFile.readlines()
      configFile.close()
    except IOError, exc:
      self.valsLock.release()
      raise exc
    
    self.valsLock.release()
  
  def isLoaded(self):
    """
    Provides true if there's loaded contents, false otherwise.
    """
    
    return self.contents != None
  
  def getConfigLocation(self):
    """
    Provides the location of the loaded configuration contents. This may be
    available, even if the torrc failed to be loaded.
    """
    
    return self.configLocation
  
  def getContents(self):
    """
    Provides the contents of the configuration file.
    """
    
    self.valsLock.acquire()
    returnVal = list(self.contents) if self.contents else None
    self.valsLock.relese()
    return returnVal
  
  def getDisplayContents(self, strip = False):
    """
    Provides the contents of the configuration file, formatted in a rendering
    frindly fashion:
    - Tabs print as three spaces. Keeping them as tabs is problematic for
      layouts since it's counted as a single character, but occupies several
      cells.
    - Strips control and unprintable characters.
    
    Arguments:
      strip - removes comments and extra whitespace if true
    """
    
    self.valsLock.acquire()
    
    if not self.isLoaded(): returnVal = None
    else:
      if self.displayableContents == None:
        # restricts contents to displayable characters
        self.displayableContents = []
        
        for lineNum in range(len(self.contents)):
          lineText = self.contents[lineNum]
          lineText = lineText.replace("\t", "   ")
          lineText = "".join([char for char in lineText if curses.ascii.isprint(char)])
          self.displayableContents.append(lineText)
      
      if strip:
        if self.strippedContents == None:
          self.strippedContents = _stripComments(self.displayableContents)
        
        returnVal = list(self.strippedContents)
      else: returnVal = list(self.displayableContents)
    
    self.valsLock.release()
    return returnVal
  
  def getCorrections(self):
    """
    Performs validation on the loaded contents and provides back the
    corrections. If validation is disabled then this won't provide any
    results.
    """
    
    self.valsLock.acquire()
    
    if not self.isLoaded(): returnVal = None
    elif not CONFIG["features.torrc.validate"]: returnVal = {}
    else:
      if self.corrections == None:
        self.corrections = validate(self.contents)
      
      returnVal = dict(self.corrections)
    
    self.valsLock.release()
    return returnVal
  
  def getLock(self):
    """
    Provides the lock governing concurrent access to the contents.
    """
    
    return self.valsLock

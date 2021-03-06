#!/usr/bin/env python
#------------------------------------------------------------
#    FILE: genmon.py
# PURPOSE: Monitor for MODBUS port on Generac Generator
#
#  AUTHOR: Jason G Yates
#    DATE: 05-Oct-2016
#
# MODIFICATIONS:
#------------------------------------------------------------


## Notes:
#   Pin 8 (white) appears to be TX from Controller
#   Pin 7 (black) appears to be TX from mobile link

# http://modbus.rapidscada.net/

from __future__ import print_function       # For python 3.x compatibility with print function

import datetime, time, sys, smtplib, signal, os, threading, socket
import atexit, json, collections, random
import httplib, re

try:
    from ConfigParser import RawConfigParser
except ImportError as e:
    from configparser import RawConfigParser

from genmonlib import myserial, mymail, mylog, mythread, mymodbus


GENMON_VERSION = "V1.6.5"

#-------------------Generator specific const defines for Generator class
LOG_DEPTH               = 50
START_LOG_STARTING_REG  = 0x012c    # the most current start log entry should be at this register
START_LOG_STRIDE        = 4
START_LOG_END_REG       = ((START_LOG_STARTING_REG + (START_LOG_STRIDE * LOG_DEPTH)) - START_LOG_STRIDE)
ALARM_LOG_STARTING_REG  = 0x03e8    # the most current alarm log entry should be at this register
ALARM_LOG_STRIDE        = 5
ALARM_LOG_END_REG       = ((ALARM_LOG_STARTING_REG + (ALARM_LOG_STRIDE * LOG_DEPTH)) - ALARM_LOG_STRIDE)
SERVICE_LOG_STARTING_REG= 0x04e2    # the most current service log entry should be at this register
SERVICE_LOG_STRIDE      = 4
SERVICE_LOG_END_REG     = ((SERVICE_LOG_STARTING_REG + (SERVICE_LOG_STRIDE * LOG_DEPTH)) - SERVICE_LOG_STRIDE)
# Register for Model number
MODEL_REG               = 0x01f4
MODEL_REG_LENGTH        = 5

NEXUS_ALARM_LOG_STARTING_REG    = 0x064
NEXUS_ALARM_LOG_STRIDE          = 4
NEXUS_ALARM_LOG_END_REG         = ((NEXUS_ALARM_LOG_STARTING_REG + (NEXUS_ALARM_LOG_STRIDE * LOG_DEPTH)) - NEXUS_ALARM_LOG_STRIDE)

DEFAULT_THRESHOLD_VOLTAGE = 143
DEFAULT_PICKUP_VOLTAGE = 190
#------------ GeneratorDevice class --------------------------------------------
class GeneratorDevice:

    def __init__(self):
        self.ProgramName = "Generator Monitor"
        self.BaudRate = 9600        # data rate of the serial port (default 9600)
        self.Registers = {}         # dict for registers and values
        self.RegistersUnderTest = {}# dict for registers we are testing
        self.RegistersUnderTestData = ""
        self.NotChanged = 0         # stats for registers
        self.Changed = 0            # stats for registers
        self.TotalChanged = 0.0     # ratio of changed ragisters
        self.LastAlarmValue = 0xFF  # Last Value of the Alarm Register
        self.ConnectionList = []    # list of incoming connections for heartbeat
        self.ServerSocket = 0       # server socket for nagios heartbeat and command/status
        self.Threads = {}           # Dict of mythread objects
        self.GeneratorInAlarm = False       # Flag to let the heartbeat thread know there is a problem
        self.SystemInOutage = False         # Flag to signal utility power is out
        self.TransferActive = False         # Flag to signal transfer switch is allowing gen supply power
        self.CommunicationsActive = False   # Flag to let the heartbeat thread know we are communicating
        self.CommAccessLock = threading.RLock()  # lock to synchronize access to the serial port comms
        self.CheckForAlarmEvent = threading.Event() # Event to signal checking for alarm
        self.UtilityVoltsMin = 0    # Minimum reported utility voltage above threshold
        self.UtilityVoltsMax = 0    # Maximum reported utility voltage above pickup
        self.MailInit = False       # set to true once mail is init
        self.InitComplete = False   # set to true once init is complete
        self.NewInstall = False     # True if newly installed or newly upgraded version
        self.FeedbackEnabled = False   # True if sending autoated feedback on missing information
        self.FeedbackMessages = {}

        self.Version = "Unknown"

        self.DaysOfWeek = { 0: "Sunday",    # decode for register values with day of week
                            1: "Monday",
                            2: "Tuesday",
                            3: "Wednesday",
                            4: "Thursday",
                            5: "Friday",
                            6: "Saturday"}
        self.MonthsOfYear = { 1: "January",     # decode for register values with month
                              2: "February",
                              3: "March",
                              4: "April",
                              5: "May",
                              6: "June",
                              7: "July",
                              8: "August",
                              9: "September",
                              10: "October",
                              11: "November",
                              12: "December"}

        # base registers and their length in bytes
        # note: the lengths are in bytes. The request packet should be in words
        # and due to the magic of python, we often deal with the response in string values
        #   dict format  Register: [ Length in bytes: monitor change 0 - no, 1 = yes]
        self.BaseRegisters = {                  # base registers read by master
                    "0000" : [2, 0],     # possibly product line code (Nexus, EvoAQ, EvoLQ)
                    "0005" : [2, 0],     # Exercise Time Hi Byte = Hour, Lo Byte = Min (Read Only) (Nexus, EvoAQ, EvoLQ)
                    "0006" : [2, 0],     # Exercise Time Hi Byte = Day of Week 00=Sunday 01=Monday, Low Byte = 00=quiet=no, 01=yes (Nexus, EvoAQ, EvoLQ)
                    "0007" : [2, 0],     # Engine RPM  (Nexus, EvoAQ, EvoLQ)
                    "0008" : [2, 0],     # Freq - value includes Hz to the tenths place i.e. 59.9 Hz (Nexus, EvoAQ, EvoLQ)
                    "000a" : [2, 0],     # battery voltage Volts to  tenths place i.e. 13.9V (Nexus, EvoAQ, EvoLQ)
                    "000b" : [2, 0],     # engine run time hours High
                    "000c" : [2, 0],     # engine run time hours Low
                    "000e" : [2, 0],     # Read / Write: Generator Time Hi byte = hours, Lo byte = min (Nexus, EvoAQ, EvoLQ)
                    "000f" : [2, 0],     # Read / Write: Generator Time Hi byte = month, Lo byte = day of the month (Nexus, EvoAQ, EvoLQ)
                    "0010" : [2, 0],     # Read / Write: Generator Time = Hi byte Day of Week 00=Sunday 01=Monday, Lo byte = last 2 digits of year (Nexus, EvoAQ, EvoLQ)
                    "0011" : [2, 0],     # Utility Threshold, ML Does not read this  (Nexus, EvoAQ, EvoLQ) (possibly read / write)
                    "0012" : [2, 0],     # Gen output voltage (Nexus, EvoAQ, EvoLQ)
                    "0019" : [2, 0],     # Model ID register, (EvoAC, NexusAC)
                    "001a" : [2, 0],     # Hours Until Service A
                    "001b" : [2, 0],     # Date Service A Due
                    "001c" : [2, 0],     # Service Info Hours (Nexus)
                    "001d" : [2, 0],     # Service Info Date (Nexus)
                    "001e" : [2, 0],     # Hours Until Service B
                    "001f" : [2, 0],     # Hours util Service (NexusAC), Date Service Due (Evo)
                    "0020" : [2, 0],     # Service Info Date (NexusAC)
                    "0021" : [2, 0],     # Service Info Hours (NexusAC)
                    "0022" : [2, 0],     # Service Info Date (NexusAC, EvoAC)
                    "002a" : [2, 0],     # hardware (high byte) (Hardware V1.04 = 0x68) and firmware version (low byte) (Firmware V1.33 = 0x85) (Nexus, EvoAQ, EvoLQ)
                    "002b" : [2, 0],     # Startup Delay (Evo AC)
                    "002c" : [2, 0],     # Evo      (Exercise Time) Exercise Time HH:MM
                    "002d" : [2, 0],     # Evo AC   (Weekly, Biweekly, Monthly)
                    "002e" : [2, 0],     # Evo      (Exercise Time) Exercise Day Sunday =0, Monday=1
                    "002f" : [2, 0],     # Evo      (Quiet Mode)
                    "0059" : [2, 0],     # Set Voltage from Dealer Menu (not currently used)
                    "023b" : [2, 0],     # Pick Up Voltage (Evo LQ only)
                    "023e" : [2, 0],     # Exercise time duration (Evo LQ only)
                    "0054" : [2, 0],     # Hours since generator activation (hours of protection) (Evo LQ only)
                    "005e" : [2, 0],     # Total engine time in minutes High (EvoLC)
                    "005f" : [2, 0],     # Total engine time in minutes Low  (EvoLC)
                    "0057" : [2, 0],     # Unknown Looks like some status bits (0002 to 0005 when engine starts, back to 0002 on stop)
                    "0055" : [2, 0],     # Unknown
                    "0056" : [2, 0],     # Unknown Looks like some status bits (0000 to 0003, back to 0000 on stop)
                    "005a" : [2, 0],     # Unknown (zero except Nexus)
                    "000d" : [2, 0],     # Bit changes when the controller is updating registers.
                    "003c" : [2, 0],     # Raw RPM Sensor Data (Hall Sensor)
                    "0058" : [2, 0],     # CT Sensor (EvoLC)
                    "005d" : [2, 0],     # Unknown sensor 3, Moves between 0x55 - 0x58 continuously even when engine off
                    "05ed" : [2, 0],     # Unknown sensor 4, changes between 35, 37, 39 (Ambient Temp Sensor) EvoLC
                    "05ee" : [2, 0],     # Unknown sensor 5 (Battery Charging Sensor)
                    "05fa" : [2, 0],     # Evo AC   (Status?)
                    "0033" : [2, 0],     # Evo AC   (Status?)
                    "0034" : [2, 0],     # Evo AC   (Status?) Goes from FFFF 0000 00001 (Nexus and Evo AC)
                    "0032" : [2, 0],     # Evo AC   (Sensor?) starts  0x4000 ramps up to ~0x02f0
                    "0036" : [2, 0],     # Evo AC   (Sensor?) Unknown
                    "0037" : [2, 0],     # CT Sensor (EvoAC)
                    "0038" : [2, 0],     # Evo AC   (Sensor?)       FFFE, FFFF, 0001, 0002 random - not linear
                    "0039" : [2, 0],     # Evo AC   (Sensor?)
                    "003a" : [2, 0],     # Evo AC   (Sensor?)  Nexus and Evo AC
                    "003b" : [2, 0],     # Evo AC   (Sensor?)  Nexus and Evo AC
                    "0239" : [2, 0],     # Startup Delay (Evo AC)
                    "0237" : [2, 0],     # Set Voltage (Evo LC)
                    "0208" : [2, 0],     # Calibrate Volts (Evo)
                    "005c" : [2, 0],     # Unknown , possible model reg on EvoLC
                    "05f3" : [2, 0],     # EvoAC, EvoLC, counter of some type
                    "05f4" : [2, 0],     # Evo AC   Current 1
                    "05f5" : [2, 0],     # Evo AC   Current 2
                    "05f6" : [2, 0],     # Evo AC   Current Cal 1
                    "05f7" : [2, 0],     # Evo AC   Current Cal 1
                    }

        # registers that need updating more frequently than others to make things more responsive
        self.PrimeRegisters = {
                    "0001" : [4, 0],     # Alarm and status register
                    "0053" : [2, 0],     # Evo LC Output relay status register (battery charging, transfer switch, Change at startup and stop
                    "0052" : [2, 0],     # Evo LC Input status register (sensors) only tested on liquid cooled Evo
                    "0009" : [2, 0],     # Utility voltage
                    "05f1" : [2, 0]}     # Last Alarm Code

        self.WriteRegisters = {  # 0003 and 0004 are index registers, used to write exercise time and other unknown stuff (remote start, stop and transfer)
                    "002c" : 2,     # Read / Write: Exercise Time HH:MM
                    "002e" : 2,     # Read / Write: Exercise Day Sunday =0, Monday=1
                    "002f" : 2}     # Read / Write: Exercise Quiet Mode=1 Not Quiet Mode = 0

        self.REGLEN = 0
        self.REGMONITOR = 1

        self.Address = 0x9d
        self.LogLocation = "/var/log/"
        self.SiteName = "Home"

        # set defaults for optional parameters
        self.bDisplayOutput = False
        self.bDisplayMonitor = False
        self.bDisplayRegisters = False
        self.bDisplayStatus = False
        self.EnableDebug = False

        self.bDisplayUnknownSensors = False
        self.bDisplayMaintenance = False
        self.bUseLegacyWrite = False
        self.EvolutionController = None
        self.LiquidCooled = None
        # The values "Unknown" are checked to validate conf file items are found
        self.FuelType = "Unknown"
        self.NominalFreq = "Unknown"
        self.NominalRPM = "Unknown"
        self.NominalKW = "Unknown"
        self.Model = "Unknown"
        self.PowerLogMaxSize = 15       # 15 MB max size
        self.PowerLog =  os.path.dirname(os.path.realpath(__file__)) + "/kwlog.txt"
        self.OutageLog = os.path.dirname(os.path.realpath(__file__)) + "/outage.txt"
        self.FeedbackLogFile = os.path.dirname(os.path.realpath(__file__)) + "/feedback.json"
        self.DisableOutageCheck = False
        self.bSyncTime = False          # Sync gen to system time
        self.bSyncDST = False           # sync time at DST change
        self.bDST = False               # Daylight Savings Time active if True
        self.bEnhancedExerciseFrequency = False     # True if controller supports biweekly and monthly exercise times

        # read config file
        if not self.GetConfig():
            return None

        # log errors in this module to a file
        self.log = mylog.SetupLogger("genmon", self.LogLocation + "genmon.log")

        if self.NewInstall:
            self.LogError("New version detected: Old = %s, New = %s" % (self.Version, GENMON_VERSION))
            self.Version = GENMON_VERSION

        self.ProgramStartTime = datetime.datetime.now()     # used for com metrics

        self.OutageStartTime = self.ProgramStartTime    # if these two are the same, no outage has occured
        self.LastOutageDuration = self.OutageStartTime - self.OutageStartTime

        atexit.register(self.Close)

        try:
            #Starting device connection
            self.ModBus = mymodbus.ModbusProtocol(self.UpdateRegisterList, self.Address, self.SerialPort, self.BaudRate, loglocation = self.LogLocation)
            self.Threads["SerialReadThread"] = self.ModBus.Slave.StartReadThread()

        except Exception as e1:
            self.FatalError("Error opening serial device: " + str(e1))
            return None

        # init mail, start processing incoming email
        self.mail = mymail.MyMail(monitor=True, incoming_folder = self.IncomingEmailFolder, processed_folder =self.ProcessedEmailFolder,incoming_callback = self.ProcessCommand)
        self.MailInit = True

        # send mail to tell we are starting
        self.mail.sendEmail("Generator Monitor Starting at " + self.SiteName, "Generator Monitor Starting at " + self.SiteName , msgtype = "info")

        # check for ALARM.txt file present
        try:
            self.AlarmFile = os.path.dirname(os.path.realpath(__file__)) + "/ALARMS.txt"
            with open(self.AlarmFile,"r") as AlarmFile:     #
                self.printToScreen("Validated alarm file present")
        except Exception as e1:
            self.FatalError("Unable to open alarm file: " + str(e1))

        if self.mail.GetSendEmailThreadObject():
            self.Threads["SendMailThread"] = self.mail.GetSendEmailThreadObject()
        if self.mail.GetEmailMonitorThreadObject():
            self.Threads["EmailCommandThread"] = self.mail.GetEmailMonitorThreadObject()

        self.ProcessFeedbackInfo()
        self.StartThreads()

        self.LogError("GenMon Loadded for site: " + self.SiteName)

    # ---------- GeneratorDevice::StartThreads------------------
    def StartThreads(self, reload = False):

        self.Threads["CheckForAlarmThread"] = mythread.MyThread(self.CheckForAlarmThread, Name = "CheckForAlarmThread")

        # start read thread to process incoming data commands
        self.Threads["ProcessThread"] = mythread.MyThread(self.ProcessThread, Name = "ProcessThread")

        # start thread to accept incoming sockets for nagios heartbeat
        self.Threads["ComWatchDog"] = mythread.MyThread(self.ComWatchDog, Name = "ComWatchDog")

        if not reload:
            # This thread remains open during a reload
            # start thread to accept incoming sockets for nagios heartbeat and command / status clients
            self.Threads["InterfaceServerThread"] = mythread.MyThread(self.InterfaceServerThread, Name = "InterfaceServerThread")

        # start thread to accept incoming sockets for nagios heartbeat
        self.Threads["PowerMeter"] = mythread.MyThread(self.PowerMeter, Name = "PowerMeter")

        # start read thread to monitor registers as they change
        self.Threads["MonitorThread"] = mythread.MyThread(self.MonitorThread, Name = "MonitorThread")

        if self.bSyncDST or self.bSyncTime:     # Sync time thread
            self.Threads["TimeSyncThread"] = mythread.MyThread(self.TimeSyncThread, Name = "TimeSyncThread")

        if self.EnableDebug:        # for debugging registers
            self.Threads["DebugThread"] = mythread.MyThread(self.DebugThread, Name = "DebugThread")

    # ---------- GeneratorDevice::KillThread------------------
    def KillThread(self, Name, CleanupSelf = False):

        try:
            MyThreadObj = self.Threads.get(Name, None)
            if MyThreadObj == None:
                del self.Threads[Name]
                self.LogError("Error getting thread name in KillThread: " + Name)
                return False

            del self.Threads[Name]
            if CleanupSelf:
                MyThreadObj.Stop()
                MyThreadObj.WaitForThreadToEnd()
        except Exception as e1:
            return

    # ---------- GeneratorDevice::KillReloadThread------------------
    def IsStopSignaled(self, Name):

        Thread = self.Threads.get(Name, None)
        if Thread == None:
            self.LogError("Error getting thread name in IsStopSignaled: " + Name)
            return False

        return Thread.StopSignaled()

    # ---------- GeneratorDevice::Reload------------------
    def Reload(self):

        try:
            RetStr = ""

            self.KillThread("ProcessThread")
            self.KillThread("MonitorThread")
            self.KillThread("CheckForAlarmThread")
            self.KillThread("PowerMeter")
            self.KillThread("ComWatchDog")
            if self.bSyncDST or self.bSyncTime:
                self.KillThread("TimeSyncThread")
            if self.EnableDebug:
                self.KillThread("DebugThread")

            if self.MailInit:
                self.mail.Cleanup()
                try:
                    del self.Threads["SendMailThread"]
                    del self.Threads["EmailCommandThread"]
                except Exception as e1:
                    RetStr = ""     # no error to report, using exception if send or rx email was disabled.


            self.MailInit = False

            if self.ModBus.DeviceInit:
                self.ModBus.Slave.Close()

            self.ModBus.DeviceInit = False

            if not self.GetConfig(reload = True):
                RetStr =  "Error reloading, error reading config file"

            # log errors in this module to a file
            self.log = mylog.SetupLogger("genmon", self.LogLocation + "genmon.log")
            try:
                #Starting device connection
                self.ModBus = mymodbus.ModbusProtocol(self.UpdateRegisterList, self.Address, self.SerialPort, self.BaudRate, loglocation = self.LogLocation)
                self.Threads["SerialReadThread"] = self.ModBus.Slave.StartReadThread()
            except Exception as e1:
                self.LogError("Error in Reload (serial): " + str(e1))
                RetStr = "Failed to reload serial port."

            # init mail, start processing incoming email
            self.mail = mymail.MyMail(monitor=True, incoming_folder = self.IncomingEmailFolder, processed_folder =self.ProcessedEmailFolder,incoming_callback = self.ProcessCommand)
            self.MailInit = True

            if self.mail.GetSendEmailThreadObject():
                self.Threads["SendMailThread"] = self.mail.GetSendEmailThreadObject()
            if self.mail.GetEmailMonitorThreadObject():
                self.Threads["EmailCommandThread"] = self.mail.GetEmailMonitorThreadObject()

            # send mail to tell we are starting again
            self.mail.sendEmail("Generator Monitor Reload at " + self.SiteName, "Generator Monitor Reload at " + self.SiteName , msgtype = "info")

            self.StartThreads(reload = True)
            self.LogError("RELOAD COMPLETE")

            if RetStr == "":
                return "Genmon reloaded"
            else:
                return RetStr

        except Exception as e1:
            self.LogError("Error in Reload: " + str(e1))
            return "Genmon failed to reload"

    # ---------- GeneratorDevice::GetConfig------------------
    def GetConfig(self, reload = False):

        ConfigSection = "GenMon"
        try:
            # read config file
            config = RawConfigParser()
            # config parser reads from current directory, when running form a cron tab this is
            # not defined so we specify the full path
            config.read('/etc/genmon.conf')

            # getfloat() raises an exception if the value is not a float
            # getint() and getboolean() also do this for their respective types

            if config.has_option(ConfigSection, 'sitename'):
                self.SiteName = config.get(ConfigSection, 'sitename')
            self.SerialPort = config.get(ConfigSection, 'port')
            self.IncomingEmailFolder = config.get(ConfigSection, 'incoming_mail_folder')     # imap folder for incoming mail
            self.ProcessedEmailFolder = config.get(ConfigSection, 'processed_mail_folder')   # imap folder for processed mail
            # heartbeat server port, must match value in check_monitor_system.py and any calling client apps
            self.ServerSocketPort = config.getint(ConfigSection, 'server_port')
            if config.has_option(ConfigSection, 'address'):
                self.Address = int(config.get(ConfigSection, 'address'),16)                      # modbus address
            if config.has_option(ConfigSection, 'loglocation'):
                self.LogLocation = config.get(ConfigSection, 'loglocation')

            # optional config parameters, by default the software will attempt to auto-detect the controller
            # this setting will override the auto detect
            if config.has_option(ConfigSection, 'evolutioncontroller'):
                self.EvolutionController = config.getboolean(ConfigSection, 'evolutioncontroller')
            if config.has_option(ConfigSection, 'liquidcooled'):
                self.LiquidCooled = config.getboolean(ConfigSection, 'liquidcooled')
            if config.has_option(ConfigSection, 'disableoutagecheck'):
                self.DisableOutageCheck = config.getboolean(ConfigSection, 'disableoutagecheck')

            if config.has_option(ConfigSection, 'displayoutput'):
                self.bDisplayOutput = config.getboolean(ConfigSection, 'displayoutput')
            if config.has_option(ConfigSection, 'displaymonitor'):
                self.bDisplayMonitor = config.getboolean(ConfigSection, 'displaymonitor')
            if config.has_option(ConfigSection, 'displayregisters'):
                self.bDisplayRegisters = config.getboolean(ConfigSection, 'displayregisters')
            if config.has_option(ConfigSection, 'displaystatus'):
                self.bDisplayStatus = config.getboolean(ConfigSection, 'displaystatus')
            if config.has_option(ConfigSection, 'displaymaintenance'):
                self.bDisplayMaintenance = config.getboolean(ConfigSection, 'displaymaintenance')
            if config.has_option(ConfigSection, 'enabledebug'):
                self.EnableDebug = config.getboolean(ConfigSection, 'enabledebug')

            if config.has_option(ConfigSection, 'displayunknown'):
                self.bDisplayUnknownSensors = config.getboolean(ConfigSection, 'displayunknown')
            if config.has_option(ConfigSection, 'uselegacysetexercise'):
                self.bUseLegacyWrite = config.getboolean(ConfigSection, 'uselegacysetexercise')
            if config.has_option(ConfigSection, 'outagelog'):
                self.OutageLog = config.get(ConfigSection, 'outagelog')
            if config.has_option(ConfigSection, 'kwlog'):
                self.PowerLog = config.get(ConfigSection, 'kwlog')
            if config.has_option(ConfigSection, 'kwlogmax'):
                self.PowerLogMaxSize = config.getint(ConfigSection, 'kwlogmax')

            if config.has_option(ConfigSection, 'syncdst'):
                self.bSyncDST = config.getboolean(ConfigSection, 'syncdst')
            if config.has_option(ConfigSection, 'synctime'):
                self.bSyncTime = config.getboolean(ConfigSection, 'synctime')
            if config.has_option(ConfigSection, 'enhancedexercise'):
                self.bEnhancedExerciseFrequency = config.getboolean(ConfigSection, 'enhancedexercise')

            if config.has_option(ConfigSection, 'nominalfrequency'):
                self.NominalFreq = config.get(ConfigSection, 'nominalfrequency')
            if config.has_option(ConfigSection, 'nominalRPM'):
                self.NominalRPM = config.get(ConfigSection, 'nominalRPM')
            if config.has_option(ConfigSection, 'nominalKW'):
                self.NominalKW = config.get(ConfigSection, 'nominalKW')
            if config.has_option(ConfigSection, 'model'):
                self.Model = config.get(ConfigSection, 'model')

            if config.has_option(ConfigSection, 'fueltype'):
                self.FuelType = config.get(ConfigSection, 'fueltype')

            if config.has_option(ConfigSection, 'version'):
                self.Version = config.get(ConfigSection, 'version')
                if not self.Version == GENMON_VERSION:
                    self.AddItemToConfFile('version', GENMON_VERSION)
                    self.NewInstall = True
            else:
                self.AddItemToConfFile('version', GENMON_VERSION)
                self.NewInstall = True
            if config.has_option(ConfigSection, "autofeedback"):
                self.FeedbackEnabled = config.getboolean(ConfigSection, 'autofeedback')
            else:
                self.AddItemToConfFile('autofeedback', "False")
                self.FeedbackEnabled = False
            # Load saved feedback log if log is present
            if os.path.isfile(self.FeedbackLogFile):
                with open(self.FeedbackLogFile) as infile:
                    self.FeedbackMessages = json.load(infile)

        except Exception as e1:
            if not reload:
                raise Exception("Missing config file or config file entries: " + str(e1))
            else:
                self.LogError("Error reloading config file" + str(e1))
            return False

        return True
    #------------------------------------------------------------
    # Add or update config item
    def AddItemToConfFile(self, Entry, Value):

        FileName = "/etc/genmon.conf"
        try:
            Found = False
            ConfigFile = open(FileName,'r')
            FileString = ConfigFile.read()
            ConfigFile.close()

            ConfigFile = open(FileName,'w')
            for line in FileString.splitlines():
                if not line.isspace():                  # blank lines
                    newLine = line.strip()              # strip leading spaces
                    if len(newLine):
                        if not newLine[0] == "#":           # not a comment
                            items = newLine.split(' ')      # split items in line by spaces
                            for strings in items:           # loop thru items
                                strings = strings.strip()   # strip any whitespace
                                if Entry == strings or strings.lower().startswith(Entry+"="):        # is this our value?
                                    line = Entry + " = " + Value    # replace it
                                    Found = True
                                    break

                ConfigFile.write(line+"\n")
            if not Found:
                ConfigFile.write(Entry + " = " + Value + "\n")
            ConfigFile.close()
            return True

        except Exception as e1:
            self.LogError("Error in AddItemToConfFile: " + str(e1))
            return False


    # ---------- GeneratorDevice::CheckForAlarmThread------------------
    #  When signaled, this thread will check for alarms
    def CheckForAlarmThread(self):

        while True:
            try:
                time.sleep(0.25)
                if self.IsStopSignaled("CheckForAlarmThread"):
                    break
                if self.CheckForAlarmEvent.is_set():
                    self.CheckForAlarmEvent.clear()
                    self.CheckForAlarms()

            except Exception as e1:
                self.FatalError("Error in  CheckForAlarmThread" + str(e1))

    # ---------- GeneratorDevice::ProcessThread------------------
    #  remove items from Buffer, form packets
    #  all read and writes to serial port(s) should occur in this thread so we can
    #  serialize access to the ports
    def ProcessThread(self):

        try:
            self.ModBus.Flush()
            self.InitDevice()
            while True:
                if self.IsStopSignaled("ProcessThread"):
                    break
                try:
                    self.MasterEmulation()
                    if self.EnableDebug:
                        self.DebugRegisters()
                except Exception as e1:
                    self.LogError("Error in GeneratorDevice:ProcessThread (1), continue: " + str(e1))
        except Exception as e1:
            self.FatalError("Exiting GeneratorDevice:ProcessThread (2)" + str(e1))

    # ---------- GeneratorDevice::MonitorThread------------------
    # This thread will analyze the cached registers. It should not write to the serial port(s)
    def MonitorThread(self):

        while True:
            try:
                time.sleep(5)
                if self.IsStopSignaled("MonitorThread"):
                    break
                if self.bDisplayMonitor:
                    self.DisplayMonitor()       # display communication stats
                if self.bDisplayRegisters:
                    self.DisplayRegisters()     # display registers
                if self.bDisplayStatus:
                    self.DisplayStatus()        # display generator engine status
                if self.bDisplayMaintenance:
                    self.DisplayMaintenance()   # display Maintenance
            except Exception as e1:
                self.LogError("Error in GeneratorDevice:MonitorThread " + str(e1))

    #-------------GeneratorDevice::InitDevice------------------------------------
    # One time reads, and read all registers once
    def InitDevice(self):

        self.ModBus.ProcessMasterSlaveTransaction("%04x" % MODEL_REG, MODEL_REG_LENGTH)

        self.DetectController()

        if self.EvolutionController:
            self.ModBus.ProcessMasterSlaveTransaction("%04x" % ALARM_LOG_STARTING_REG, ALARM_LOG_STRIDE)
        else:
            self.ModBus.ProcessMasterSlaveTransaction("%04x" % NEXUS_ALARM_LOG_STARTING_REG, NEXUS_ALARM_LOG_STRIDE)

        self.ModBus.ProcessMasterSlaveTransaction("%04x" % START_LOG_STARTING_REG, START_LOG_STRIDE)

        if self.EvolutionController:
            self.ModBus.ProcessMasterSlaveTransaction("%04x" % SERVICE_LOG_STARTING_REG, SERVICE_LOG_STRIDE)

        for PrimeReg, PrimeInfo in self.PrimeRegisters.items():
            self.ModBus.ProcessMasterSlaveTransaction(PrimeReg, int(PrimeInfo[self.REGLEN] / 2))

        for Reg, Info in self.BaseRegisters.items():

            #The divide by 2 is due to the diference in the values in our dict are bytes
            # but modbus makes register request in word increments so the request needs to
            # in word multiples, not bytes
            self.ModBus.ProcessMasterSlaveTransaction(Reg, int(Info[self.REGLEN] / 2))

        # check for model specific info in read from conf file, if not there then add some defaults
        self.CheckModelSpecificInfo()

        self.InitComplete = True

         # check for unknown events (i.e. events we are not decoded) and send an email if they occur
        self.CheckForAlarmEvent.set()

    #------------------------------------------------------------
    def ProcessFeedbackInfo(self):

        if self.FeedbackEnabled:
            for Key, Entry in self.FeedbackMessages.items():
                self.mail.sendEmail("Generator Monitor Submission", Entry , recipient = "generatormonitor.software@gmail.com", msgtype = "error")
            # delete unsent Messages
            if os.path.isfile(self.FeedbackLogFile):
                os.remove(self.FeedbackLogFile)

    #------------------------------------------------------------
    def SendFeedbackInfo(self, Reason, Always = False, Message = None, FullLogs = False):
        try:
            if self.NewInstall or Always:

                CheckedSent = self.FeedbackMessages.get(Reason, "")

                if not CheckedSent == "":
                    return

                msgbody = "Reason = " + Reason + "\n"
                if Message != None:
                    msgbody += "Message : " + Message + "\n"
                msgbody += "Version: " + GENMON_VERSION
                msgbody += self.DisplayRegisters(AllRegs = FullLogs, ToString = True)
                if self.FeedbackEnabled:
                    self.mail.sendEmail("Generator Monitor Submission", msgbody , recipient = "generatormonitor.software@gmail.com", msgtype = "error")

                self.FeedbackMessages[Reason] = msgbody
                # if feedback not enabled, save the log to file
                if not self.FeedbackEnabled:
                    with open(self.FeedbackLogFile, 'w') as outfile:
                        json.dump(self.FeedbackMessages, outfile, sort_keys = True, indent = 4, ensure_ascii = False)
        except Exception as e1:
            self.LogError("Error in SendFeedbackInfo: " + str(e1))

    #------------------------------------------------------------
    def CheckModelSpecificInfo(self):

        if self.NominalFreq == "Unknown" or not len(self.NominalFreq):
            self.NominalFreq = self.GetModelInfo("Frequency")
            if self.NominalFreq == "Unknown":
                self.NominalFreq = "60"
            self.AddItemToConfFile("nominalfrequency", self.NominalFreq)

        # This is not correct for 50Hz models
        if self.NominalRPM == "Unknown" or not len(self.NominalRPM):
            if self.LiquidCooled:
                if self.NominalFreq == "50":
                    self.NominalRPM = "1500"
                else:
                    self.NominalRPM = "1800"
            else:
                if self.NominalFreq == "50":
                    self.NominalRPM = "3000"
                else:
                    self.NominalRPM = "3600"
            self.AddItemToConfFile("nominalRPM", self.NominalRPM)

        TempStr = self.GetModelInfo("KW")
        if TempStr == "Unknown":
            self.SendFeedbackInfo("ModelID", Message="Model ID register is unknown")

        if self.NominalKW == "Unknown" or self.Model == "Unknown" or not len(self.NominalKW) or not len(self.Model) or self.NewInstall:

            self.NominalKW = self.GetModelInfo("KW")

            if not self.LookUpSNInfo(SkipKW = (not self.NominalKW == "Unknown")):
                if self.LiquidCooled:
                    self.Model = "Generic Liquid Cooled"
                    if self.NominalKW == "Unknown":
                        self.NominalKW = "60"
                else:
                    self.Model = "Generic Air Cooled"
                    if self.NominalKW == "Unknown":
                        self.NominalKW = "22"
            self.AddItemToConfFile("model", self.Model)
            self.AddItemToConfFile("nominalKW", self.NominalKW)

        if self.FuelType == "Unknown" or not len(self.FuelType):
            if self.Model.startswith("RD"):
                self.FuelType = "Diesel"
            elif self.Model.startswith("RG") or self.Model.startswith("QT"):
                self.FuelType = "Natural Gas"
            elif self.LiquidCooled and self.EvolutionController:          # EvoLC
                self.FuelType = "Diesel"
            else:
                self.FuelType = "Natural Gas"                           # NexusLC, NexusAC, EvoAC
            self.AddItemToConfFile("fueltype", self.FuelType)

    #------------ GeneratorDevice::GetModelInfo-------------------------------
    def GetModelInfo(self, Request):

        UnknownList = ["Unknown", "Unknown", "Unknown", "Unknown"]

        # Nexus LQ is the QT line
        # 50Hz : QT02724MNAX
        # QT022, QT027, QT036, QT048, QT080, QT070,QT100,QT130,QT150
        ModelLookUp_NexusLC = {}

        # Nexus AC
        ModelLookUp_NexusAC = {
                                0 : ["8KW", "60", "120/240", "1"],
                                2 : ["14KW", "60", "120/240", "1"],
                                4 : ["20KW", "60", "120/240", "1"]
                                }
        # This should cover the guardian line
        ModelLookUp_EvoAC = { #ID : [KW or KVA Rating, Hz Rating, Voltage Rating, Phase]
                                1 : ["9KW", "60", "120/240", "1"],
                                2 : ["14KW", "60", "120/240", "1"],
                                3 : ["17KW", "60", "120/240", "1"],
                                4 : ["20KW", "60", "120/240", "1"],
                                5 : ["8KW", "60", "120/240", "1"],
                                7 : ["13KW", "60", "120/240", "1"],
                                8 : ["15KW", "60", "120/240", "1"],
                                9 : ["16KW", "60", "120/240", "1"],
                                10 : ["20KW", "VSCF", "120/240", "1"],    #Variable Speed Constant Frequency
                                11 : ["15KW", "ECOVSCF", "120/240", "1"], # Eco Variable Speed Constant Frequency
                                12 : ["8KVA", "50", "220,230,240", "1"],         # 3 distinct models 220, 230, 240
                                13 : ["10KVA", "50", "220,230,240", "1"],         # 3 distinct models 220, 230, 240
                                14 : ["13KVA", "50", "220,230,240", "1"],        # 3 distinct models 220, 230, 240
                                15 : ["11KW", "60" ,"240", "1"],
                                17 : ["22KW", "60", "120/240", "1"],
                                21 : ["11KW", "60", "240 LS", "1"],
                                32 : ["Trinity", "60", "208 3Phase", "3"],      # G007077
                                33 : ["Trinity", "50", "380,400,416", "3"]       # 3 distinct models 380, 400 or 416
                                }

        # Evolution LC is the Protector series
        # 50Hz Models: RG01724MNAX, RG02224MNAX, RG02724RNAX
        # RG022, RG025,RG030,RG027,RG036,RG032,RG045,RG038,RG048,RG060
        # RD01523,RD02023,RD03024,RD04834,RD05034
        ModelLookUp_EvoLC = {
                                13: ["48KW", "60", "120/240", "1"]
                            }
        Register = "None"
        LookUp = None
        if not self.LiquidCooled:
            Register = "0019"
            if self.EvolutionController:
                LookUp = ModelLookUp_EvoAC
            else:
                LookUp = ModelLookUp_NexusAC
        elif self.EvolutionController and self.LiquidCooled:
            Register = "005c"
            LookUp = ModelLookUp_EvoLC
        else:
            LookUp = ModelLookUp_NexusLC
            return "Unknown"    # Nexus LC is not known

        Value = self.GetRegisterValueFromList(Register)
        if not len(Value):
            return "Unknown"

        ModelInfo = LookUp.get(int(Value,16), UnknownList)

        if Request.lower() == "frequency":
            if ModelInfo[1] == "60" or ModelInfo[1] == "50":
                return ModelInfo[1]

        elif Request.lower() == "kw":
            if "kw" in ModelInfo[0].lower():
                return self.removeAlpha(ModelInfo[0])
            elif "kva" in ModelInfo[0].lower():
                # TODO: This is not right, I think if we take KVA * 0.8 it should equal KW for single phase
                return self.removeAlpha(ModelInfo[0])
            else:
                return "Unknown"

        elif Request.lower() == "phase":
            return ModelInfo[3]

        return "Unknown"

    #------------------------------------------------------------
    def LookUpSNInfo(self, SkipKW = False):

        productId = None
        ModelNumber = None

        SerialNumber = self.GetSerialNumber()
        Controller = self.GetController()

        if not len(SerialNumber) or not len(Controller):
            self.LogError("Error in LookUpSNInfo: bad input")
            return False

        if "None" in SerialNumber.lower():      # serial number is not present due to controller being replaced
            return False

        try:
            # for diagnostic reasons we will log the internet search
            self.LogError("Looking up model info on internet")
            myregex = re.compile('<.*?>')

            try:
                conn = httplib.HTTPSConnection("www.generac.com", 443, timeout=10)
                conn.request("GET", "/GeneracCorporate/WebServices/GeneracSelfHelpWebService.asmx/GetSearchResults?query=" + SerialNumber, "",
                        headers={"User-Agent": "Mozilla/4.0 (compatible; MSIE 5.01; Windows NT 5.0)"})
                r1 = conn.getresponse()
            except Exception as e1:
                conn.close()
                self.LogError("Error in LookUpSNInfo (request 1): " + str(e1))
                return False

            try:
                data1 = r1.read()
                data2 = re.sub(myregex, '', data1)
                myresponse1 = json.loads(data2)
                ModelNumber = myresponse1["SerialNumber"]["ModelNumber"]

                if not len(ModelNumber):
                    self.LogError("Error in LookUpSNInfo: Model (response1)")
                    conn.close()
                    return False

                self.LogError("Found: Model: %s" % str(ModelNumber))
                self.Model = ModelNumber

            except Exception as e1:
                self.LogError("Error in LookUpSNInfo (parse request 1): " + str(e1))
                conn.close()
                return False

            try:
                productId = myresponse1["Results"][0]["Id"]
            except Exception as e1:
                self.LogError("Note LookUpSNInfo (parse request 1), (product ID not found): " + str(e1))
                productId = SerialNumber

            if SkipKW:
                return True

            try:
                if productId == SerialNumber:
                    conn.request("GET", "/service-support/product-support-lookup/product-manuals?modelNo="+productId, "",
                    headers={"User-Agent": "Mozilla/4.0 (compatible; MSIE 5.01; Windows NT 5.0)"})
                else:
                    conn.request("GET", "/GeneracCorporate/WebServices/GeneracSelfHelpWebService.asmx/GetProductById?productId="+productId, "",
                        headers={"User-Agent": "Mozilla/4.0 (compatible; MSIE 5.01; Windows NT 5.0)"})
                r1 = conn.getresponse()
                data1 = r1.read()
                conn.close()
                data2 = re.sub(myregex, '', data1)
            except Exception as e1:
                self.LogError("Error in LookUpSNInfo (parse request 2, product ID): " + str(e1))

            try:
                if productId == SerialNumber:
                    #within the formatted HTML we are looking for something like this :   "Manuals: 17KW/990 HNYWL+200A SE"
                    ListData = re.split("<div", data1) #
                    for Count in range(len(ListData)):
                        if "Manuals:" in ListData[Count]:
                            KWStr = re.findall(r"(\d+)KW", ListData[Count])[0]
                            if len(KWStr) and KWStr.isdigit():
                                self.NominalKW = KWStr

                else:
                    myresponse2 = json.loads(data2)

                    kWRating = myresponse2["Attributes"][0]["Value"]

                    if "kw" in kWRating.lower():
                        kWRating = self.removeAlpha(kWRating)
                    elif "watts" in kWRating.lower():
                        kWRating = self.removeAlpha(kWRating)
                        kWRating = str(int(kWRating) / 1000)
                    else:
                        kWRating = str(int(kWRating) / 1000)

                    self.NominalKW = kWRating

                    if not len(kWRating):
                        self.LogError("Error in LookUpSNInfo: KW")
                        return False

                    self.LogError("Found: KW: %skW" % str(kWRating))

            except Exception as e1:
                self.LogError("Error in LookUpSNInfo: (parse KW)" + str(e1))
                return False

            return True
        except Exception as e1:
            self.LogError("Error in LookUpSNInfo: " + str(e1))
            return False


    #-------------GeneratorDevice::DetectController------------------------------------
    def DetectController(self):

        # issue modbus read
        self.ModBus.ProcessMasterSlaveTransaction("0000", 1)

        # read register from cached list.
        Value = self.GetRegisterValueFromList("0000")
        if len(Value) != 4:
            return ""
        ProductModel = int(Value,16)

        # 0x03  Nexus, Air Cooled
        # 0x06  Nexus, Liquid Cooled
        # 0x09  Evolution, Air Cooled
        # 0x0c  Evolution, Liquid Cooled

        msgbody = "\nThis email is a notification informing you that the software has detected a generator "
        msgbody += "model variant that has not been validated by the authors of this sofrware. "
        msgbody += "The software has made it's best effort to identify your generator controller type however since "
        msgbody += "your generator is one that we have not validated, your generator controller may be incorrectly identified. "
        msgbody += "To validate this variant, please submit the output of the following command (generator: registers)"
        msgbody += "and your model numbert to the following project thread: https://github.com/jgyates/genmon/issues/10. "
        msgbody += "Once your feedback is receivd we an add your model product code and controller type to the list in the software."

        if self.EvolutionController == None:

            # if reg 000 is 3 or less then assume we have a Nexus Controller
            if ProductModel == 0x03 or ProductModel == 0x06:
                self.EvolutionController = False    #"Nexus"
                self.printToScreen("Nexus Controller Detected")
            elif ProductModel == 0x09 or ProductModel == 0x0c:
                self.EvolutionController = True     #"Evolution"
                self.printToScreen("Evolution Controller Detected")
            else:
                # set a reasonable default
                if ProductModel <= 0x06:
                    self.EvolutionController = False
                else:
                    self.EvolutionController = True

                self.LogError("Warning in DetectController (Nexus / Evolution):  Unverified value detected in model register (%04x)" %  ProductModel)
                self.mail.sendEmail("Generator Monitor (Nexus / Evolution): Warning at " + self.SiteName, msgbody, msgtype = "warn" )
        else:
            self.LogError("DetectController auto-detect override (controller). EvolutionController now is %s" % str(self.EvolutionController))

        if self.LiquidCooled == None:
            if ProductModel == 0x03 or ProductModel == 0x09:
                self.LiquidCooled = False    # Air Cooled
                self.printToScreen("Air Cooled Model Detected")
            elif ProductModel == 0x06 or ProductModel == 0x0c:
                self.LiquidCooled = True     # Liquid Cooled
                self.printToScreen("Liquid Cooled Model Detected")
            else:
                # set a reasonable default
                self.LiquidCooled = False
                self.LogError("Warning in DetectController (liquid / air cooled):  Unverified value detected in model register (%04x)" %  ProductModel)
                self.mail.sendEmail("Generator Monitor (liquid / air cooled: Warning at " + self.SiteName, msgbody, msgtype = "warn" )
        else:
            self.LogError("DetectController auto-detect override (Liquid Cooled). Liquid Cooled now is %s" % str(self.LiquidCooled))

        if not self.EvolutionController:        # if we are using a Nexus Controller, force legacy writes
            self.bUseLegacyWrite = True

    #----------  GeneratorDevice:GetController  ---------------------------------
    def GetController(self, Actual = True):

        outstr = ""

        if Actual:

            ControllerDecoder = {
                0x03 :  "Nexus, Air Cooled",
                0x06 :  "Nexus, Liquid Cooled",
                0x09 :  "Evolution, Air Cooled",
                0x0c :  "Evolution, Liquid Cooled"
            }

            Value = self.GetRegisterValueFromList("0000")
            if len(Value) != 4:
                return ""
            ProductModel = int(Value,16)

            return ControllerDecoder.get(ProductModel, "Unknown 0x%02X" % ProductModel)
        else:

            if self.EvolutionController:
                outstr = "Evolution, "
            else:
                outstr = "Nexus, "
            if self.LiquidCooled:
                outstr += "Liquid Cooled"
            else:
                outstr += "Air Cooled"

        return outstr

    #-------------GeneratorDevice::DebugRegisters------------------------------------
    def DebugRegisters(self):

        # reg 200 - -3e7 and 4af - 4e2 and 5af - 600 (already got 5f1 5f4 and 5f5?
        for Reg in range(0x05 , 0x700):
            RegStr = "%04x" % Reg
            if not self.RegisterIsKnown(RegStr):
                self.ModBus.ProcessMasterSlaveTransaction(RegStr, 1)

    #-------------GeneratorDevice::MasterEmulation------------------------------------
    def MasterEmulation(self):

        counter = 0
        for Reg, Info in self.BaseRegisters.items():

            if counter % 6 == 0:
                for PrimeReg, PrimeInfo in self.PrimeRegisters.items():
                    self.ModBus.ProcessMasterSlaveTransaction(PrimeReg, int(PrimeInfo[self.REGLEN] / 2))
                # check for unknown events (i.e. events we are not decoded) and send an email if they occur
                self.CheckForAlarmEvent.set()

            #The divide by 2 is due to the diference in the values in our dict are bytes
            # but modbus makes register request in word increments so the request needs to
            # in word multiples, not bytes
            self.ModBus.ProcessMasterSlaveTransaction(Reg, int(Info[self.REGLEN] / 2))
            counter += 1

     #-------------GeneratorDevice::UpdateLogRegistersAsMaster
    def UpdateLogRegistersAsMaster(self):

        # Start / Stop Log
        for Register in self.LogRange(START_LOG_STARTING_REG , LOG_DEPTH,START_LOG_STRIDE):
            RegStr = "%04x" % Register
            self.ModBus.ProcessMasterSlaveTransaction(RegStr, START_LOG_STRIDE)

        if self.EvolutionController:
            # Service Log
            for Register in self.LogRange(SERVICE_LOG_STARTING_REG , LOG_DEPTH, SERVICE_LOG_STRIDE):
                RegStr = "%04x" % Register
                self.ModBus.ProcessMasterSlaveTransaction(RegStr, SERVICE_LOG_STRIDE)

            # Alarm Log
            for Register in self.LogRange(ALARM_LOG_STARTING_REG , LOG_DEPTH, ALARM_LOG_STRIDE):
                RegStr = "%04x" % Register
                self.ModBus.ProcessMasterSlaveTransaction(RegStr, ALARM_LOG_STRIDE)
        else:
            # Alarm Log
            for Register in self.LogRange(NEXUS_ALARM_LOG_STARTING_REG , LOG_DEPTH, NEXUS_ALARM_LOG_STRIDE):
                RegStr = "%04x" % Register
                self.ModBus.ProcessMasterSlaveTransaction(RegStr, NEXUS_ALARM_LOG_STRIDE)

     #----------  GeneratorDevice::SetGeneratorRemoteStartStop-------------------------------
    def SetGeneratorRemoteStartStop(self, CmdString):

        msgbody = "Invalid command syntax for command setremote (1)"

        try:
            #Format we are looking for is "setremote=start"
            CmdList = CmdString.split("=")
            if len(CmdList) != 2:
                self.LogError("Validation Error: Error parsing command string in SetGeneratorRemoteStartStop (parse): " + CmdString)
                return msgbody

            CmdList[0] = CmdList[0].strip()

            if not CmdList[0].lower() == "setremote":
                self.LogError("Validation Error: Error parsing command string in SetGeneratorRemoteStartStop (parse2): " + CmdString)
                return msgbody

            Command = CmdList[1].strip()

        except Exception as e1:
            self.LogError("Validation Error: Error parsing command string in SetGeneratorRemoteStartStop: " + CmdString)
            self.LogError( str(e1))
            return msgbody

        # Index register 0001 controls remote start (data written 0001 to start,I believe ).
        # Index register 0002 controls remote transfer switch (Not sure of the data here )
        Register = 0
        Value = 0x000               # writing any value to index register is valid for remote start / stop commands

        if Command == "start":
            Register = 0x0001       # remote start (radio start)
        elif Command == "stop":
            Register = 0x0000       # remote stop (radio stop)
        elif Command == "starttransfer":
            Register = 0x0002       # start the generator, then engage the transfer transfer switch
        elif Command == "startexercise":
            Register = 0x0003       # remote run in quiet mode (exercise)
        else:
            return "Invalid command syntax for command setremote (2)"

        with self.CommAccessLock:
            #
            LowByte = Value & 0x00FF
            HighByte = Value >> 8
            Data= []
            Data.append(HighByte)           # Value for indexed register (High byte)
            Data.append(LowByte)            # Value for indexed register (Low byte)

            self.ModBus.ProcessMasterSlaveWriteTransaction("0004", len(Data) / 2, Data)

            LowByte = Register & 0x00FF
            HighByte = Register >> 8
            Data= []
            Data.append(HighByte)           # indexed register to be written (High byte)
            Data.append(LowByte)            # indexed register to be written (Low byte)

            self.ModBus.ProcessMasterSlaveWriteTransaction("0003", len(Data) / 2, Data)

        return "Remote command sent successfully"

    #-------------MonitorUnknownRegisters--------------------------------------------------------
    def MonitorUnknownRegisters(self,Register, FromValue, ToValue):


        msgbody = ""
        if self.RegisterIsKnown(Register):
            if not self.MonitorRegister(Register):
                return

            msgbody = "%s changed from %s to %s" % (Register, FromValue, ToValue)
            msgbody += "\n"
            msgbody += self.DisplayRegisters(ToString = True)
            msgbody += "\n"
            msgbody += self.DisplayStatus(ToString = True)

            self.mail.sendEmail("Monitor Register Alert: " + Register, msgbody, msgtype = "warn")
        else:
            # bulk register monitoring goes here and an email is sent out in a batch
            if self.EnableDebug:
                BitsChanged, Mask = self.GetNumBitsChanged(FromValue, ToValue)
                self.RegistersUnderTestData += "Reg %s changed from %s to %s, Bits Changed: %d, Mask: %x, Engine State: %s\n" % \
                        (Register, FromValue, ToValue, BitsChanged, Mask, self.GetEngineState())

    #----------  GeneratorDevice::GetNumBitsChanged-------------------------------
    def GetNumBitsChanged(self, FromValue, ToValue):

        MaskBitsChanged = int(FromValue, 16) ^ int(ToValue, 16)
        NumBitsChanged = MaskBitsChanged
        count = 0
        while (NumBitsChanged):
            count += NumBitsChanged & 1
            NumBitsChanged >>= 1

        return count, MaskBitsChanged

    #----------  GeneratorDevice::CalculateExerciseTime-------------------------------
    # helper routine for AltSetGeneratorExerciseTime
    def CalculateExerciseTime(self,MinutesFromNow):

        ReturnedValue = 0x00
        Remainder = MinutesFromNow
        # convert minutes from now to weighted bit value
        if Remainder >= 8738:
            ReturnedValue |= 0x1000
            Remainder -=  8738
        if Remainder >= 4369:
            ReturnedValue |= 0x0800
            Remainder -=  4369
        if Remainder >= 2184:
            ReturnedValue |= 0x0400
            Remainder -=  2185
        if Remainder >= 1092:
            ReturnedValue |= 0x0200
            Remainder -=  1092
        if Remainder >= 546:
            ReturnedValue |= 0x0100
            Remainder -=  546
        if Remainder >= 273:
            ReturnedValue |= 0x0080
            Remainder -=  273
        if Remainder >= 136:
            ReturnedValue |= 0x0040
            Remainder -=  137
        if Remainder >= 68:
            ReturnedValue |= 0x0020
            Remainder -=  68
        if Remainder >= 34:
            ReturnedValue |= 0x0010
            Remainder -=  34
        if Remainder >= 17:
            ReturnedValue |= 0x0008
            Remainder -=  17
        if Remainder >= 8:
            ReturnedValue |= 0x0004
            Remainder -=  8
        if Remainder >= 4:
            ReturnedValue |= 0x0002
            Remainder -=  4
        if Remainder >= 2:
            ReturnedValue |= 0x0001
            Remainder -=  2

        return ReturnedValue

    #----------  GeneratorDevice::AltSetGeneratorExerciseTime-------------------------------
    # Note: This method is a bit odd but it is how ML does it. It can result in being off by
    # a min or two
    def AltSetGeneratorExerciseTime(self, CmdString):

        # extract time of day and day of week from command string
        # format is day:hour:min  Monday:15:00
        msgsubject = "Generator Command Notice at " + self.SiteName
        msgbody = "Invalid command syntax for command setexercise"
        try:

            DayOfWeek =  {  "monday": 0,        # decode for register values with day of week
                            "tuesday": 1,       # NOTE: This decodes for datetime i.e. Monday=0
                            "wednesday": 2,     # the generator firmware programs Sunday = 0, but
                            "thursday": 3,      # this is OK since we are calculating delta minutes
                            "friday": 4,        # since time of day to set exercise time
                            "saturday": 5,
                            "sunday": 6}

            Day, Hour, Minute, ModeStr = self.ParseExerciseStringEx(CmdString, DayOfWeek)

        except Exception as e1:
            self.LogError("Validation Error: Error parsing command string in AltSetGeneratorExerciseTime: " + CmdString)
            self.LogError( str(e1))
            return msgbody

        if Minute < 0 or Hour < 0 or Day < 0:     # validate settings
            self.LogError("Validation Error: Error parsing command string in AltSetGeneratorExerciseTime (v1): " + CmdString)
            return msgbody

        if not ModeStr.lower() in ["weekly"]:
            self.LogError("Validation Error: Error parsing command string in AltSetGeneratorExerciseTime (v2): " + CmdString)
            return msgbody

        # Get System time and create a new datatime item with the target exercise time
        GeneratorTime = datetime.datetime.strptime(self.GetDateTime(), "%A %B %d, %Y %H:%M")
        # fix hours and min in gen time to the requested exercise time
        TargetExerciseTime = GeneratorTime.replace(hour = Hour, minute = Minute, day = GeneratorTime.day)
        # now change day of week
        while TargetExerciseTime.weekday() != Day:
            TargetExerciseTime += datetime.timedelta(1)

        # convert total minutes between two datetime objects
        DeltaTime =  TargetExerciseTime - GeneratorTime
        total_delta_min = self.GetDeltaTimeMinutes(DeltaTime)

        WriteValue = self.CalculateExerciseTime(total_delta_min)

        with self.CommAccessLock:
            #  have seen the following values 0cf6,0f8c,0f5e
            Last = WriteValue & 0x00FF
            First = WriteValue >> 8
            Data= []
            Data.append(First)             # Hour 0 - 23
            Data.append(Last)             # Min 0 - 59

            self.ModBus.ProcessMasterSlaveWriteTransaction("0004", len(Data) / 2, Data)

            #
            Data= []
            Data.append(0)                  # The value for reg 0003 is always 0006. This appears
            Data.append(6)                  # to be an indexed register

            self.ModBus.ProcessMasterSlaveWriteTransaction("0003", len(Data) / 2, Data)
        return  "Set Exercise Time Command sent (using legacy write)"

    #----------  GeneratorDevice::GetDeltaTimeMinutes-------------------------------
    def GetDeltaTimeMinutes(self, DeltaTime):

        days, seconds = DeltaTime.days, DeltaTime.seconds
        delta_hours = days * 24 + seconds // 3600
        delta_minutes = (seconds % 3600) // 60

        return (delta_hours * 60 + delta_minutes)

    #----------  GeneratorDevice::SetGeneratorExerciseTime-------------------------------
    def SetGeneratorExerciseTime(self, CmdString):

        # use older style write to set exercise time if this flag is set
        if self.bUseLegacyWrite:
            return self.AltSetGeneratorExerciseTime(CmdString)


        # extract time of day and day of week from command string
        # format is day:hour:min  Monday:15:00
        msgbody = "Invalid command syntax for command setexercise"
        try:

            DayOfWeek =  {  "sunday": 0,
                            "monday": 1,        # decode for register values with day of week
                            "tuesday": 2,       # NOTE: This decodes for datetime i.e. Sunday = 0, Monday=1
                            "wednesday": 3,     #
                            "thursday": 4,      #
                            "friday": 5,        #
                            "saturday": 6,
                            }

            Day, Hour, Minute, ModeStr = self.ParseExerciseStringEx(CmdString, DayOfWeek)

        except Exception as e1:
            self.LogError("Validation Error: Error parsing command string in SetGeneratorExerciseTime: " + CmdString)
            self.LogError( str(e1))
            return msgbody

        if Minute < 0 or Hour < 0 or Day < 0:     # validate Settings
            self.LogError("Validation Error: Error parsing command string in SetGeneratorExerciseTime (v1): " + CmdString)
            return msgbody


        # validate conf file option
        if not self.bEnhancedExerciseFrequency:
            if ModeStr.lower() in ["biweekly", "monthly"]:
                self.LogError("Validation Error: Biweekly and Monthly Exercises are not supported. " + CmdString)
                return msgbody

        with self.CommAccessLock:

            if self.bEnhancedExerciseFrequency:
                Data = []
                Data.append(0x00)
                if ModeStr.lower() == "weekly":
                    Data.append(0x00)
                elif ModeStr.lower() == "biweekly":
                    Data.append(0x01)
                elif ModeStr.lower() == "monthly":
                    Data.append(0x02)
                else:
                    self.LogError("Validation Error: Invalid exercise frequency. " + CmdString)
                    return msgbody
                self.ModBus.ProcessMasterSlaveWriteTransaction("002d", len(Data) / 2, Data)

            Data = []
            Data.append(0x00)               #
            Data.append(Day)                # Day

            self.ModBus.ProcessMasterSlaveWriteTransaction("002e", len(Data) / 2, Data)

            #
            Data = []
            Data.append(Hour)                  #
            Data.append(Minute)                #

            self.ModBus.ProcessMasterSlaveWriteTransaction("002c", len(Data) / 2, Data)

        return  "Set Exercise Time Command sent"

    #----------  GeneratorDevice::ParseExerciseStringEx-------------------------------
    def ParseExerciseStringEx(self, CmdString, DayDict):

        Day = -1
        Hour = -1
        Minute = -1
        ModeStr = ""
        try:

            #Format we are looking for is :
            # "setexercise=Monday,12:20"  (weekly default)
            # "setexercise=Monday,12:20,weekly"
            # "setexercise=Monday,12:20,biweekly"
            # "setexercise=15,12:20,monthly"

            if "setexercise" not in  CmdString.lower():
                self.LogError("Validation Error: Error parsing command string in ParseExerciseStringEx (setexercise): " + CmdString)
                return Day, Hour, Minute, ModeStr

            Items = CmdString.split(b"=")

            if len(Items) != 2:
                self.LogError("Validation Error: Error parsing command string in ParseExerciseStringEx (command): " + CmdString)
                return Day, Hour, Minute, ModeStr

            ParsedItems = Items[1].split(b",")

            if len(ParsedItems) < 2 or len(ParsedItems) > 3:
                self.LogError("Validation Error: Error parsing command string in ParseExerciseStringEx (items): " + CmdString)
                return Day, Hour, Minute, ModeStr

            DayStr = ParsedItems[0].strip()

            if len(ParsedItems) == 3:
                ModeStr = ParsedItems[2].strip()
            else:
                ModeStr = "weekly"

            if ModeStr.lower() not in ["weekly", "biweekly", "monthly"]:
                self.LogError("Validation Error: Error parsing command string in ParseExerciseStringEx (Mode): " + CmdString)
                return Day, Hour, Minute, ModeStr

            TimeItems = ParsedItems[1].split(b":")

            if len(TimeItems) != 2:
                return Day, Hour, Minute, ModeStr

            HourStr = TimeItems[0].strip()

            MinuteStr = TimeItems[1].strip()

            Minute = int(MinuteStr)
            Hour = int(HourStr)

            if ModeStr.lower() != "monthly":
                Day = DayDict.get(DayStr.lower(), -1)
                if Day == -1:
                    self.LogError("Validation Error: Error parsing command string in ParseExerciseStringEx (day of week): " + CmdString)
                    return -1, -1, -1, ""
            else:
                Day = int(DayStr.lower())

        except Exception as e1:
            self.LogError("Validation Error: Error parsing command string in ParseExerciseStringEx: " + CmdString)
            self.LogError( str(e1))
            return -1, -1, -1, ""

        if not ModeStr.lower() in ["weekly", "biweekly", "monthly"]:
            self.LogError("Validation Error: Error parsing command string in ParseExerciseStringEx (v2): " + CmdString)
            return -1, -1, -1, ""

        if Minute < 0 or Hour < 0 or Day < 0:     # validate Settings
            self.LogError("Validation Error: Error parsing command string in ParseExerciseStringEx (v3): " + CmdString)
            return -1, -1, -1, ""

        if ModeStr.lower() in ["weekly", "biweekly"]:
            if Minute >59 or Hour > 23 or Day > 6:     # validate Settings
                self.LogError("Validation Error: Error parsing command string in ParseExerciseStringEx (v4): " + CmdString)
                return -1, -1, -1, ""
        else:
            if Minute >59 or Hour > 23 or Day > 28:    # validate Settings
                self.LogError("Validation Error: Error parsing command string in ParseExerciseStringEx (v5): " + CmdString)
                return -1, -1, -1, ""

        return Day, Hour, Minute, ModeStr

     #----------  GeneratorDevice::SetGeneratorQuietMode-------------------------------
    def SetGeneratorQuietMode(self, CmdString):

        # extract quiet mode setting from Command String
        # format is setquiet=yes or setquiet=no
        msgbody = "Invalid command syntax for command setquiet"
        try:
            # format is setquiet=yes or setquiet=no
            CmdList = CmdString.split("=")
            if len(CmdList) != 2:
                self.LogError("Validation Error: Error parsing command string in SetGeneratorQuietMode (parse): " + CmdString)
                return msgbody

            CmdList[0] = CmdList[0].strip()

            if not CmdList[0].lower() == "setquiet":
                self.LogError("Validation Error: Error parsing command string in SetGeneratorQuietMode (parse2): " + CmdString)
                return msgbody

            Mode = CmdList[1].strip()

            if "on" in Mode.lower():
                ModeValue = 0x01
            elif "off" in Mode.lower():
                ModeValue = 0x00
            else:
                self.LogError("Validation Error: Error parsing command string in SetGeneratorQuietMode (value): " + CmdString)
                return msgbody

        except Exception as e1:
            self.LogError("Validation Error: Error parsing command string in SetGeneratorQuietMode: " + CmdString)
            self.LogError( str(e1))
            return msgbody

        Data= []
        Data.append(0x00)
        Data.append(ModeValue)
        with self.CommAccessLock:
            self.ModBus.ProcessMasterSlaveWriteTransaction("002f", len(Data) / 2, Data)

        return "Set Quiet Mode Command sent"

    #----------  GeneratorDevice::SetGeneratorTimeDate-------------------------------
    def SetGeneratorTimeDate(self):

        # get system time
        d = datetime.datetime.now()

        # attempt to make the seconds zero when we set the generator time so it will
        # be very close to the system time
        # Testing has show that this is not really achieving the seconds synced up, but
        # it does make the time offset consistant
        while d.second != 0:
            time.sleep(60 - d.second)       # sleep until seconds are zero
            d = datetime.datetime.now()

        # We will write three registers at once: 000e - 0010.
        Data= []
        Data.append(d.hour)             #000e
        Data.append(d.minute)
        Data.append(d.month)            #000f
        Data.append(d.day)
        # Note: Day of week should always be zero when setting time
        Data.append(0)                  #0010
        Data.append(d.year - 2000)

        self.ModBus.ProcessMasterSlaveWriteTransaction("000e", len(Data) / 2, Data)

    #------------ GeneratorDevice::GetRegisterLength --------------------------------------------
    def GetRegisterLength(self, Register):

        RegInfoReg = self.BaseRegisters.get(Register, [0,0])

        RegLength = RegInfoReg[self.REGLEN]

        if RegLength == 0:
            RegInfoReg = self.PrimeRegisters.get(Register, [0,0])
            RegLength = RegInfoReg[self.REGLEN]

        return RegLength

    #------------ GeneratorDevice::MonitorRegister --------------------------------------------
    # return true if we are monitoring this register
    def MonitorRegister(self, Register):

        RegInfoReg = self.BaseRegisters.get(Register, [0,-1])

        MonitorReg = RegInfoReg[self.REGMONITOR]

        if MonitorReg == -1:
            RegInfoReg = self.PrimeRegisters.get(Register, [0,-1])
            MonitorReg = RegInfoReg[self.REGMONITOR]

        if MonitorReg == 1:
            return True
        return False

    #------------ GeneratorDevice::ValidateRegister --------------------------------------------
    def ValidateRegister(self, Register, Value):

        ValidationOK = True
        # validate the length of the data against the size of the register
        RegLength = self.GetRegisterLength(Register)
        if(RegLength):      # if this is a base register
            if RegLength != (len(Value) / 2):  # note: the divide here compensates between the len of hex values vs string data
                self.LogError("Validation Error: Invalid register length (base) %s:%s %d %d" % (Register, Value, RegLength, len(Value) /2 ))
                ValidationOK = False
        # appears to be Start/Stop Log or service log
        elif int(Register,16) >=  SERVICE_LOG_STARTING_REG and int(Register,16) <= SERVICE_LOG_END_REG:
            if len(Value) != 16:
                self.LogError("Validation Error: Invalid register length (Service) %s %s" % (Register, Value))
                ValidationOK = False
        elif int(Register,16) >=  START_LOG_STARTING_REG and int(Register,16) <= START_LOG_END_REG:
            if len(Value) != 16:
                self.LogError("Validation Error: Invalid register length (Start) %s %s" % (Register, Value))
                ValidationOK = False
        elif int(Register,16) >=  ALARM_LOG_STARTING_REG and int(Register,16) <= ALARM_LOG_END_REG:
            if len(Value) != 20:      #
                self.LogError("Validation Error: Invalid register length (Alarm) %s %s" % (Register, Value))
                ValidationOK = False
        elif int(Register,16) >=  NEXUS_ALARM_LOG_STARTING_REG and int(Register,16) <= NEXUS_ALARM_LOG_END_REG:
            if len(Value) != 16:      # Nexus alarm reg is 16 chars, no alarm codes
                self.LogError("Validation Error: Invalid register length (Nexus Alarm) %s %s" % (Register, Value))
                ValidationOK = False
        elif int(Register,16) == MODEL_REG:
            if len(Value) != 20:
                self.LogError("Validation Error: Invalid register length (Model) %s %s" % (Register, Value))
                ValidationOK = False
        else:
            self.LogError("Validation Error: Invalid register or length (Unkown) %s %s" % (Register, Value))
            ValidationOK = False

        return ValidationOK


    #------------ GeneratorDevice::RegisterIsLog --------------------------------------------
    def RegisterIsLog(self, Register):

        ## Is this a log register
        if int(Register,16) >=  SERVICE_LOG_STARTING_REG and int(Register,16) <= SERVICE_LOG_END_REG and self.EvolutionController:
            return True
        elif int(Register,16) >=  START_LOG_STARTING_REG and int(Register,16) <= START_LOG_END_REG:
            return True
        elif int(Register,16) >=  ALARM_LOG_STARTING_REG and int(Register,16) <= ALARM_LOG_END_REG and self.EvolutionController:
            return True
        elif int(Register,16) >=  NEXUS_ALARM_LOG_STARTING_REG and int(Register,16) <= NEXUS_ALARM_LOG_END_REG and (not self.EvolutionController):
            return True
        elif int(Register,16) == MODEL_REG:
            return True
        return False

    #------------ GeneratorDevice::UpdateRegisterList --------------------------------------------
    def UpdateRegisterList(self, Register, Value):

        # Validate Register by length
        if len(Register) != 4 or len(Value) < 4:
            self.LogError("Validation Error: Invalid data in UpdateRegisterList: %s %s" % (Register, Value))

        if self.RegisterIsKnown(Register):
            if not self.ValidateRegister(Register, Value):
                return
            RegValue = self.Registers.get(Register, "")

            if RegValue == "":
                self.Registers[Register] = Value        # first time seeing this register so add it to the list
            elif RegValue != Value:
                # don't print values of registers we have validated the purpose
                if not self.RegisterIsLog(Register):
                    self.MonitorUnknownRegisters(Register,RegValue, Value)
                self.Registers[Register] = Value
                self.Changed += 1
            else:
                self.NotChanged += 1
        else:   # Register Under Test
            RegValue = self.RegistersUnderTest.get(Register, "")
            if RegValue == "":
                self.RegistersUnderTest[Register] = Value        # first time seeing this register so add it to the list
            elif RegValue != Value:
                self.MonitorUnknownRegisters(Register,RegValue, Value)
                self.RegistersUnderTest[Register] = Value        # update the value

    #------------ GeneratorDevice::RegisterIsKnown ------------------------------------
    def RegisterIsKnown(self, Register):

        RegLength = self.GetRegisterLength(Register)

        if RegLength != 0:
            return True

        return self.RegisterIsLog(Register)

    #------------ GeneratorDevice::GetRegisterValueFromList ------------------------------------
    def GetRegisterValueFromList(self,Register):

        return self.Registers.get(Register, "")

    #------------ GeneratorDevice::RegRegValue ------------------------------------
    def GetRegValue(self, CmdString):

        # extract quiet mode setting from Command String
        # format is setquiet=yes or setquiet=no
        msgbody = "Invalid command syntax for command getregvalue"
        try:
            #Format we are looking for is "getregvalue=01f4"
            CmdList = CmdString.split("=")
            if len(CmdList) != 2:
                self.LogError("Validation Error: Error parsing command string in GetRegValue (parse): " + CmdString)
                return msgbody

            CmdList[0] = CmdList[0].strip()

            if not CmdList[0].lower() == "getregvalue":
                self.LogError("Validation Error: Error parsing command string in GetRegValue (parse2): " + CmdString)
                return msgbody

            Register = CmdList[1].strip()

            RegValue = self.GetRegisterValueFromList(Register)

            if RegValue == "":
                self.LogError("Validation Error: Register  not known:" + Register)
                msgbody = "Unsupported Register: " + Register
                return msgbody

            msgbody = RegValue

        except Exception as e1:
            self.LogError("Validation Error: Error parsing command string in GetRegValue: " + CmdString)
            self.LogError( str(e1))
            return msgbody

        return msgbody


    #------------ GeneratorDevice::ReadRegValue ------------------------------------
    def ReadRegValue(self, CmdString):

        # extract quiet mode setting from Command String
        #Format we are looking for is "readregvalue=01f4"
        msgbody = "Invalid command syntax for command readregvalue"
        try:

            CmdList = CmdString.split("=")
            if len(CmdList) != 2:
                self.LogError("Validation Error: Error parsing command string in ReadRegValue (parse): " + CmdString)
                return msgbody

            CmdList[0] = CmdList[0].strip()

            if not CmdList[0].lower() == "readregvalue":
                self.LogError("Validation Error: Error parsing command string in ReadRegValue (parse2): " + CmdString)
                return msgbody

            Register = CmdList[1].strip()

            RegValue = self.ModBus.ProcessMasterSlaveTransaction( Register, 1, ReturnValue = True)

            if RegValue == "":
                self.LogError("Validation Error: Register  not known (ReadRegValue):" + Register)
                msgbody = "Unsupported Register: " + Register
                return msgbody

            msgbody = RegValue

        except Exception as e1:
            self.LogError("Validation Error: Error parsing command string in ReadRegValue: " + CmdString)
            self.LogError( str(e1))
            return msgbody

        return msgbody
    #------------ GeneratorDevice::DisplayRegisters --------------------------------------------
    def DisplayRegisters(self, AllRegs = False, ToString = False, DictOut = False):

        Registers = collections.OrderedDict()
        Regs = collections.OrderedDict()
        Registers["Registers"] = Regs

        RegList = []

        Regs["Num Regs"] = "%d" % len(self.Registers)
        if self.NotChanged == 0:
            self.TotalChanged = 0.0
        else:
            self.TotalChanged =  float(self.Changed)/float(self.NotChanged)
        Regs["Not Changed"] = "%d" % self.NotChanged
        Regs["Changed"] = "%d" % self.Changed
        Regs["Total Changed"] = "%.2f" % self.TotalChanged

        Regs["Base Registers"] = RegList
        # print all the registers
        for Register, Value in self.Registers.items():

            # do not display log registers or model register
            if self.RegisterIsLog(Register):
                continue
            ##
            RegList.append({Register:Value})

        Register = "%04x" % MODEL_REG
        Value = self.GetRegisterValueFromList(Register)
        if len(Value) != 0:
            RegList.append({Register:Value})

        if AllRegs:
            Regs["Log Registers"]= self.DisplayLogs(AllLogs = True, RawOutput = True, DictOut = True)

        if not DictOut:
            return self.printToScreen(self.ProcessDispatch(Registers,""), ToString)

        return Registers

     #---------- process command from email and socket -------------------------------
    def ProcessCommand(self, command, fromsocket = False):

        LocalError = False

        msgsubject = "Generator Command Response at " + self.SiteName
        if not fromsocket:
            msgbody = "\n"
        else:
            msgbody = ""

        if(len(command)) == 0:
            msgsubject = "Error in Generator Command (Lenght is zero)"
            msgbody += "Invalid GENERATOR command: zero length command. All commands must be prefixed by \"generator: \""
            LocalError = True

        if not LocalError:
            if(not command.lower().startswith( b'generator:' )):         # PYTHON3
                msgsubject = "Error in Generator Command (no generator: prefix)"
                self.printToScreen("Invalid GENERATOR command")
                msgbody += "Invalid GENERATOR command: all commands must be prefixed by \"generator: \""
                LocalError = True

        if LocalError:
            if not fromsocket:
                self.mail.sendEmail(msgsubject, msgbody, msgtype = "error")
                return ""       # ignored by email module
            else:
                msgbody += "EndOfMessage"
                return msgbody

        if command.lower().startswith(b'generator:'):
            command = command[len('generator:'):]

        CommandList = command.split(b' ')    # PYTHON3

        for item in CommandList:
            item = item.strip()
            if b"generator:" == item.lower():
                continue
            elif b"registers" == item.lower():         # display registers
                msgbody += self.DisplayRegisters(ToString = True)
                continue
            elif b"allregs" == item.lower():         # display registers
                msgbody += self.DisplayRegisters(AllRegs = True, ToString = True)
                continue
            elif b"logs" == item.lower():
                msgbody += self.DisplayLogs(AllLogs = True, ToString = True)
                continue
            elif b"status" == item.lower():            # display decoded generator info
                msgbody += self.DisplayStatus(True)
                continue
            elif b"maint" == item.lower():
                msgbody += self.DisplayMaintenance(True)
                continue
            elif b"monitor" == item.lower():
                msgbody += self.DisplayMonitor(True)
                continue
            elif b"outage" == item.lower():              # display help screen
                msgbody += self.DisplayOutage(True)
                continue
            elif b"settime" == item.lower():           # set time and date
                # This is done is a separate thread as not to block any return email processing
                # since we attempt to sync with generator time
                SetTimeThread = threading.Thread(target=self.SetGeneratorTimeDate, name = "SetTimeThread")
                SetTimeThread.daemon = True
                SetTimeThread.start()               # start settime thread
                msgbody += "Time Set: Command Sent\n"
                continue
            elif b"setexercise" in item.lower():
                msgbody += self.SetGeneratorExerciseTime( command.lower())
                continue
            elif b"setquiet" in item.lower():
                msgbody += self.SetGeneratorQuietMode( command.lower())
                continue
            elif b"help" == item.lower():              # display help screen
                msgbody += "Help:\n"
                msgbody += self.DisplayHelp(True)
                continue
            elif b"setremote" in item.lower():
                msgbody += self.SetGeneratorRemoteStartStop(command.lower())
                continue
            ## These commands are used by the web / socket interface only
            if fromsocket:
                if b"power_log_json" in item.lower():      # used in web interface
                    msgbody += json.dumps(self.GetPowerHistory(command.lower()))
                    continue
                elif b"power_log_clear" == item.lower():     # used in web interface
                    msgbody += self.ClearPowerLog()
                    continue
                elif b"start_info_json" == item.lower():      # used in web interface
                    msgbody += json.dumps(self.GetStartInfo())
                    continue
                elif b"registers_json" == item.lower():         # display registers
                    msgbody = json.dumps(self.DisplayRegisters(DictOut = True), sort_keys=False)
                    continue
                elif b"allregs_json" == item.lower():         # display registers
                    msgbody = json.dumps(self.DisplayRegisters(AllRegs = True, DictOut = True), sort_keys=False)
                    continue
                elif b"logs_json" == item.lower():
                    msgbody = json.dumps(self.DisplayLogs(AllLogs = True, DictOut = True), sort_keys=False)
                    continue
                elif b"status_json" == item.lower():            # display decoded generator info
                    msgbody = json.dumps(self.DisplayStatus(DictOut = True), sort_keys=False)
                    continue
                elif b"maint_json" == item.lower():
                    msgbody = json.dumps(self.DisplayMaintenance(DictOut = True), sort_keys=False)
                    continue
                elif b"monitor_json" == item.lower():
                    msgbody = json.dumps(self.DisplayMonitor(DictOut = True), sort_keys=False)
                    continue
                elif b"outage_json" == item.lower():              # display help screen
                    msgbody = json.dumps(self.DisplayOutage(DictOut = True), sort_keys=False)
                    continue
                if b"gui_status_json" == item.lower():          # used in web interface
                    msgbody += json.dumps(self.GetStatusForGUI())
                    continue
                if b"getsitename" == item.lower():          # used in web interface
                    msgbody += self.SiteName
                    continue
                elif b"getbase" == item.lower():            # base status, used in web interface (UI changes color based on exercise, running , ready status)
                    msgbody += self.GetBaseStatus()
                    continue
                elif b"gethealth" == item.lower():          # base status, used in web interface (UI changes color based on exercise, running , ready status)
                    msgbody += self.GetSystemHealth()
                    continue
                elif b"getexercise" == item.lower():
                    msgbody += self.GetParsedExerciseTime() # used in web interface
                    continue
                elif b"getregvalue" in item.lower():          # only used for debug purposes, read a cached register value
                    msgbody += self.GetRegValue(command.lower())
                    continue
                elif b"readregvalue" in item.lower():         # only used for debug purposes, Read Register Non Cached
                    msgbody += self.ReadRegValue(command.lower())
                    continue
                elif b"getdebug" == item.lower():              # only used for debug purposes. If a thread crashes it tells you the thread name
                    msgbody += self.GetDeadThreadName()
                    continue
                elif b"reload" == item.lower():
                    msgbody += self.Reload()
            if not fromsocket:
                msgbody += "\n\n"

        if not fromsocket:
            self.mail.sendEmail(msgsubject, msgbody, msgtype = "warn")
            return ""       # ignored by email module
        else:
            msgbody += "EndOfMessage"
            return msgbody

    #------------ GeneratorDevice::CheckForOutage ----------------------------------------
    # also update min and max utility voltage
    def CheckForOutage(self):

        if self.DisableOutageCheck:
            # do not check for outage
            return ""

        Value = self.GetRegisterValueFromList("0009")
        if len(Value) != 4:
            return ""           # we don't have a value for this register yet
        UtilityVolts = int(Value, 16)

        # Get threshold voltage
        Value = self.GetRegisterValueFromList("0011")
        if len(Value) != 4:
            return ""           # we don't have a value for this register yet
        ThresholdVoltage = int(Value, 16)

        # get pickup voltage
        if self.EvolutionController and self.LiquidCooled:
            Value = self.GetRegisterValueFromList("023b")
            if len(Value) != 4:
                return ""           # we don't have a value for this register yet
            PickupVoltage = int(Value, 16)
        else:
            PickupVoltage = DEFAULT_PICKUP_VOLTAGE

        # if something is wrong then we use some sensible values here
        if PickupVoltage == 0:
            PickupVoltage = DEFAULT_PICKUP_VOLTAGE
        if ThresholdVoltage == 0:
            ThresholdVoltage = DEFAULT_THRESHOLD_VOLTAGE

        # first time thru set the values to the same voltage level
        if self.UtilityVoltsMin == 0 and self.UtilityVoltsMax == 0:
            self.UtilityVoltsMin = UtilityVolts
            self.UtilityVoltsMax = UtilityVolts

        if UtilityVolts > self.UtilityVoltsMax:
            if UtilityVolts > PickupVoltage:
                self.UtilityVoltsMax = UtilityVolts

        if UtilityVolts < self.UtilityVoltsMin:
            if UtilityVolts > ThresholdVoltage:
                self.UtilityVoltsMin = UtilityVolts

        TransferStatus = self.GetTransferStatus()

        if len(TransferStatus):
            if self.TransferActive:
                if TransferStatus == "Utility":
                    self.TransferActive = False
                    msgbody = "\nPower is being supplied by the utility line. "
                    self.mail.sendEmail("Transfer Switch Changed State Notice at " + self.SiteName, msgbody, msgtype = "outage")
            else:
                if TransferStatus == "Generator":
                    self.TransferActive = True
                    msgbody = "\nPower is being supplied by the generator. "
                    self.mail.sendEmail("Transfer Switch Changed State Notice at " + self.SiteName, msgbody, msgtype = "outage")

        # Check for outage
        # are we in an outage now
        # NOTE: for now we are just comparing these numbers, the generator has a programmable delay
        # that must be met once the voltage passes the threshold. This may cause some "switch bounce"
        # testing needed
        if self.SystemInOutage:
            if UtilityVolts > PickupVoltage:
                self.SystemInOutage = False
                self.LastOutageDuration = datetime.datetime.now() - self.OutageStartTime
                OutageStr = str(self.LastOutageDuration).split(".")[0]  # remove microseconds from string
                msgbody = "\nUtility Power Restored. Duration of outage " + OutageStr
                self.mail.sendEmail("Outage Recovery Notice at " + self.SiteName, msgbody, msgtype = "outage")
                # log outage to file
                self.LogToFile(self.OutageLog, self.OutageStartTime.strftime("%Y-%m-%d %H:%M:%S"), OutageStr)
        else:
            if UtilityVolts < ThresholdVoltage:
                self.SystemInOutage = True
                self.OutageStartTime = datetime.datetime.now()
                msgbody = "\nUtility Power Out at " + self.OutageStartTime.strftime("%Y-%m-%d %H:%M:%S")
                self.mail.sendEmail("Outage Notice at " + self.SiteName, msgbody, msgtype = "outage")

    #------------ GeneratorDevice::LogToFile-------------------------
    def LogToFile(self, File, TimeDate, Value):

        if not len(File):
            return ""

        try:
            with open(File,"a") as LogFile:     #opens file
                LogFile.write(TimeDate + "," + Value + "\n")
                LogFile.flush()
        except Exception as e1:
            self.LogError("Error in  LogToFile : File: %s: %s " % (File,str(e1)))

    #------------ GeneratorDevice::CheckForAlarms ----------------------------------------
    # Note this must be called from the Process thread since it queries the log registers
    # when in master emulation mode
    def CheckForAlarms(self):

        # update outage time, update utility low voltage and high voltage
        self.CheckForOutage()

        # now check to see if there is an alarm
        Value = self.GetRegisterValueFromList("0001")
        if len(Value) != 8:
            return ""           # we don't have a value for this register yet
        RegVal = int(Value, 16)

        if RegVal == self.LastAlarmValue:
            return      # nothing new to report, return

        # if we get past this point there is something to report, either first time through
        # or there is an alarm that has been set or reset
        self.LastAlarmValue = RegVal    # update the stored alarm

        self.UpdateLogRegistersAsMaster()       # Update all log registers

        # Create notice email strings
        msgsubject = ""
        msgbody = "\n\n"
        msgbody += self.printToScreen("Notice from Generator: \n", True)

         # get switch state
        Value = self.GetSwitchState()
        if len(Value):
            msgbody += self.printToScreen("Switch State: " + Value, True)
        #get Engine state
        # This reports on the state read at the beginning of the routine which fixes a
        # race condition when switching from starting to running
        Value = self.GetEngineState(RegVal)
        if len(Value):                          #
            msgbody += self.printToScreen("Engine State: " + Value, True)

        if self.EvolutionController and self.LiquidCooled:
            msgbody += self.printToScreen("Active Relays: " + self.GetDigitalOutputs(), True)
            msgbody += self.printToScreen("Active Sensors: " + self.GetSensorInputs(), True)

        if self.SystemInAlarm():        # Update Alarm Status global flag, returns True if system in alarm

            msgsubject += "Generator Alert at " + self.SiteName + ": "
            AlarmState = self.GetAlarmState()

            msgsubject += "CRITICAL "
            if len(AlarmState):
                msgbody += self.printToScreen("\nCurrent Alarm: " + AlarmState , True)
            else:
                msgbody += self.printToScreen("\nSystem In Alarm! Please check alarm log", True)

            msgbody += self.printToScreen("System In Alarm: 0001:%08x" % RegVal, True)
        else:

            msgsubject = "Generator Notice: " + self.SiteName
            msgbody += self.printToScreen("\nNo Alarms: 0001:%08x" % RegVal, True)


        # send email notice
        msgbody += self.printToScreen("\nLast Log Entries:", True)

        # display last log entries
        msgbody += self.DisplayLogs(AllLogs = False, ToString = True)     # if false don't display full logs

        if self.SystemInAlarm():
            msgbody += self.printToScreen("\nTo clear the Alarm/Warning message, press OFF on the control panel keypad followed by the ENTER key.", True)

        self.mail.sendEmail(msgsubject , msgbody, msgtype = "warn")

    #------------ GeneratorDevice::DisplayHelp ----------------------------------------
    def DisplayHelp(self, ToString = False):

        outstring = self.printToScreen("\nCommands:", ToString)
        outstring += self.printToScreen("   status      - display engine and line information", ToString)
        outstring += self.printToScreen("   maint       - display maintenance and service information", ToString)
        outstring += self.printToScreen("   outage      - display current and last outage (since program launched)", ToString)
        outstring += self.printToScreen("                       info, also shows utility min and max values", ToString)
        outstring += self.printToScreen("   monitor     - display communication statistics and monitor health", ToString)
        outstring += self.printToScreen("   logs        - display all alarm, on/off, and maintenance logs", ToString)
        outstring += self.printToScreen("   registers   - display contents of registers being monitored", ToString)
        outstring += self.printToScreen("   settime     - set generator time to system time", ToString)
        outstring += self.printToScreen("   setexercise - set the exercise time of the generator. ", ToString)
        outstring += self.printToScreen("                      i.e. setexercise=Monday,13:30,Weekly", ToString)
        if self.bEnhancedExerciseFrequency:
            outstring += self.printToScreen("                      i.e. setexercise=Monday,13:30,BiWeekly", ToString)
            outstring += self.printToScreen("                      i.e. setexercise=15,13:30,Monthly", ToString)
        outstring += self.printToScreen("   setquiet    - enable or disable exercise quiet mode, ", ToString)
        outstring += self.printToScreen("                      i.e.  setquiet=on or setquiet=off", ToString)
        outstring += self.printToScreen("   setremote   - issue remote command. format is setremote=command, ", ToString)
        outstring += self.printToScreen("                      where command is start, stop, starttransfer,", ToString)
        outstring += self.printToScreen("                      startexercise. i.e. setremote=start", ToString)
        outstring += self.printToScreen("   help        - Display help on commands", ToString)
        outstring += self.printToScreen("\n", ToString)

        outstring += self.printToScreen("To clear the Alarm/Warning message, press OFF on the control panel keypad", ToString)
        outstring += self.printToScreen("followed by the ENTER key. To access Dealer Menu on the Evolution", ToString)
        outstring += self.printToScreen("controller, from the top menu selection (SYSTEM, DATE/TIME,BATTERY, SUB-MENUS)", ToString)
        outstring += self.printToScreen("enter UP UP ESC DOWN UP ESC UP, then go to the dealer menu and press enter.", ToString)
        outstring += self.printToScreen("For liquid cooled models a level 2 dealer code can be entered, ESC UP UP DOWN", ToString)
        outstring += self.printToScreen("DOWN ESC ESC, then navigate to the dealer menu and press enter.", ToString)
        outstring += self.printToScreen("Passcode for Nexus controller is ESC, UP, UP ESC, DOWN, UP, ESC, UP, UP, ENTER.", ToString)
        outstring += self.printToScreen("\n", ToString)

        return outstring

    #------------ GeneratorDevice::GetDispatchItem ------------------------------------
    def GetDispatchItem(self, item):

        if isinstance(item, str):
            return item
        if isinstance(item, unicode):
            return str(item)
        elif callable(item):
            return item()
        elif isinstance(item, (int, long)):
            return str(item)
        else:
            self.LogError("Unable to convert type %s in GetDispatchItem" % type(item))
            self.LogError("Item: " + str(item))
            return ""

    #------------ GeneratorDevice::ProcessDispatch ------------------------------------
    # This function is recursive, it will turn a dict with callable functions into
    # all of the callable functions resolved to stings (by calling the functions).
    # If string output is needed instead of a dict output, ProcessDispatchToString
    # is called
    def ProcessDispatch(self, node, InputBuffer, indent=0):

        if isinstance(InputBuffer, str):
            return self.ProcessDispatchToString(node, InputBuffer, indent)

        if isinstance(node, dict):
            for key, item in node.items():
                if isinstance(item, dict):
                    NewDict = collections.OrderedDict()
                    InputBuffer[key] = self.ProcessDispatch(item, NewDict)
                elif isinstance(item, list):
                    InputBuffer[key] = []
                    for listitem in item:
                        if isinstance(listitem, dict):
                            NewDict2 = collections.OrderedDict()
                            InputBuffer[key].append(self.ProcessDispatch(listitem, NewDict2))
                        else:
                            self.LogError("Invalid type in ProcessDispatch %s " % type(node))
                else:
                    InputBuffer[key] = self.GetDispatchItem(item)
        else:
            self.LogError("Invalid type in ProcessDispatch %s " % type(node))

        return InputBuffer

     #------------ GeneratorDevice::ProcessDispatchToString -----------------------------
     # This function is recursive, it will turn a dict with callable functions into
     # a printable string with indentation and formatting
    def ProcessDispatchToString(self, node, InputBuffer, indent = 0):

        if not isinstance(InputBuffer, str):
            return ""

        if isinstance(node, dict):
            for key, item in node.items():
                if isinstance(item, dict):
                    InputBuffer += "\n" + ("    " * indent) + key + " : \n"
                    InputBuffer = self.ProcessDispatchToString(item, InputBuffer, indent + 1)
                elif isinstance(item, list):
                    InputBuffer += "\n" + ("    " * indent) + key + " : \n"
                    for listitem in item:
                        if isinstance(listitem, dict):
                            InputBuffer = self.ProcessDispatchToString(listitem, InputBuffer, indent + 1)
                        elif isinstance(listitem, str):
                            InputBuffer += (("    " * (indent +1)) +  self.GetDispatchItem(listitem) + "\n")
                        else:
                            self.LogError("Invalid type in ProcessDispatchToString %s %s (2)" % (key, type(listitem)))
                else:
                    InputBuffer += (("    " * indent) + key + " : " +  self.GetDispatchItem(item) + "\n")
        else:
            self.LogError("Invalid type in ProcessDispatchToString %s " % type(node))
        return InputBuffer

    #------------------- GeneratorDevice::DisplayOutage -----------------
    def DisplayOutage(self, ToString = False, DictOut = False):

        Outage = collections.OrderedDict()
        OutageData = collections.OrderedDict()
        Outage["Outage"] = OutageData


        if self.SystemInOutage:
            outstr = "System in outage since %s" % self.OutageStartTime.strftime("%Y-%m-%d %H:%M:%S")
        else:
            if self.ProgramStartTime != self.OutageStartTime:
                OutageStr = str(self.LastOutageDuration).split(".")[0]  # remove microseconds from string
                outstr = "Last outage occurred at %s and lasted %s." % (self.OutageStartTime.strftime("%Y-%m-%d %H:%M:%S"), OutageStr)
            else:
                outstr = "No outage has occurred since program launched."

        OutageData["Status"] = outstr

         # get utility voltage
        Value = self.GetUtilityVoltage()
        if len(Value):
            OutageData["Utility Voltage"] = Value

        OutageData["Utility Voltage Minimum"] = "%dV " % (self.UtilityVoltsMin)
        OutageData["Utility Voltage Maximum"] = "%dV " % (self.UtilityVoltsMax)

        OutageData["Utility Threshold Voltage"] = self.GetThresholdVoltage

        if self.EvolutionController and self.LiquidCooled:
            OutageData["Utility Pickup Voltage"] = self.GetPickUpVoltage

        if self.EvolutionController:
            OutageData["Startup Delay"] = self.GetStartupDelay

        OutageData["Outage Log"] = self.DisplayOutageHistory()

        if not DictOut:
            return self.printToScreen(self.ProcessDispatch(Outage,""), ToString)

        return Outage

    #------------ GeneratorDevice::DisplayOutageHistory-------------------------
    def DisplayOutageHistory(self):

        LogHistory = []

        if not len(self.OutageLog):
            return ""
        try:
            # check to see if a log file exist yet
            if not os.path.isfile(self.OutageLog):
                return ""

            OutageLog = []

            with open(self.OutageLog,"r") as OutageFile:     #opens file

                for line in OutageFile:
                    line = line.strip()                   # remove whitespace at beginning and end

                    if not len(line):
                        continue
                    if line[0] == "#":              # comment?
                        continue
                    Items = line.split(",")
                    if len(Items) != 2 and len(Items) != 3:
                        continue
                    if len(Items) == 3:
                        strDuration = Items[1] + "," + Items[2]
                    else:
                        strDuration = Items[1]

                    OutageLog.insert(0, [Items[0], strDuration])
                    if len(OutageLog) > 50:     # limit log to 50 entries
                        OutageLog.pop()

            for Items in OutageLog:
                LogHistory.append("%s, Duration: %s" % (Items[0], Items[1]))

            return LogHistory

        except Exception as e1:
            self.LogError("Error in  DisplayOutageHistory: " + str(e1))
            return []

    #------------ GeneratorDevice::DisplayMonitor --------------------------------------------
    def DisplayMonitor(self, ToString = False, DictOut = False):

        Monitor = collections.OrderedDict()
        MonitorData = collections.OrderedDict()
        Monitor["Monitor"] = MonitorData
        GenMonStats = collections.OrderedDict()
        SerialStats = collections.OrderedDict()
        MonitorData["Generator Monitor Stats"] = GenMonStats
        MonitorData["Serial Stats"] = SerialStats

        GenMonStats["Monitor Health"] =  self.GetSystemHealth()
        GenMonStats["Controller"] = self.GetController(Actual = False)


        ProgramRunTime = datetime.datetime.now() - self.ProgramStartTime
        outstr = str(ProgramRunTime).split(".")[0]  # remove microseconds from string
        GenMonStats["Run time"] = self.ProgramName + " running for " + outstr + "."
        GenMonStats["Generator Monitor Version"] = GENMON_VERSION


        SerialStats["Packet Count"] = "M: %d, S: %d, Buffer Count: %d" % (self.ModBus.Slave.TxPacketCount, self.ModBus.Slave.RxPacketCount, len(self.ModBus.Slave.Buffer))

        if self.ModBus.Slave.CrcError == 0 or self.ModBus.Slave.RxPacketCount == 0:
            PercentErrors = 0.0
        else:
            PercentErrors = float(self.ModBus.Slave.CrcError) / float(self.ModBus.Slave.RxPacketCount)

        SerialStats["CRC Errors"] = "%d " % self.ModBus.Slave.CrcError
        SerialStats["CRC Percent Errors"] = "%.2f" % PercentErrors
        SerialStats["Discarded Bytes"] = "%d" % self.ModBus.Slave.DiscardedBytes
        SerialStats["Serial Restarts"] = "%d" % self.ModBus.Slave.Restarts
        SerialStats["Serial Timeouts"] = "%d" %  self.ModBus.Slave.ComTimoutError

        CurrentTime = datetime.datetime.now()

        #
        Delta = CurrentTime - self.ModBus.Slave.SerialStartTime        # yields a timedelta object
        PacketsPerSecond = float((self.ModBus.Slave.TxPacketCount + self.ModBus.Slave.RxPacketCount)) / float(Delta.total_seconds())
        SerialStats["Packets Per Second"] = "%.2f" % (PacketsPerSecond)

        if self.ModBus.Slave.RxPacketCount:
            AvgTransactionTime = float(self.ModBus.Slave.TotalElapsedPacketeTime / self.ModBus.Slave.RxPacketCount)
            SerialStats["Average Transaction Time"] = "%.4f sec" % (AvgTransactionTime)

        if not DictOut:
            return self.printToScreen(self.ProcessDispatch(Monitor,""), ToString)

        return Monitor

    #------------ GeneratorDevice::DisplayStatus ----------------------------------------
    def DisplayStatus(self, ToString = False, DictOut = False):


        if DictOut:
            ToString = True

        Status = collections.OrderedDict()
        Stat = collections.OrderedDict()
        Status["Status"] = Stat
        Engine = collections.OrderedDict()
        Stat["Engine"] = Engine
        Line = collections.OrderedDict()
        Stat["Line State"] = Line
        LastLog = collections.OrderedDict()
        Stat["Last Log Entries"] = self.DisplayLogs(AllLogs = False, DictOut = True)
        Time = collections.OrderedDict()
        Stat["Time"] = Time


        Engine["Switch State"] = self.GetSwitchState
        Engine["Engine State"] = self.GetEngineState
        if self.EvolutionController and self.LiquidCooled:
            Engine["Active Relays"] = self.GetDigitalOutputs
            Engine["Active Sensors"] = self.GetSensorInputs

        if self.SystemInAlarm():
            Engine["System In Alarm"] = self.GetAlarmState

        Engine["Battery Voltage"] = self.GetBatteryVoltage
        if self.EvolutionController and self.LiquidCooled:
            Engine["Battery Status"] = self.GetBatteryStatus

        Engine["RPM"] = self.GetRPM

        Engine["Frequency"] = self.GetFrequency
        Engine["Output Voltage"] = self.GetVoltageOutput

        if self.EvolutionController and self.LiquidCooled:
            Engine["Output Current"] = self.GetCurrentOutput()
            Engine["Output Power (Single Phase)"] = self.GetPowerOutput()

        Engine["Active Rotor Poles (Calculated)"] = self.GetActiveRotorPoles()

        if self.bDisplayUnknownSensors:
            Engine["Unsupported Sensors"] = self.DisplayUnknownSensors()


        if self.EvolutionController:
            Line["Transfer Switch State"] = self.GetTransferStatus
        Line["Utility Voltage"] = self.GetUtilityVoltage
        #
        Line["Utility Voltage Max"] = "%dV " % (self.UtilityVoltsMax)
        Line["Utility Voltage Min"] = "%dV " % (self.UtilityVoltsMin)
        Line["Utility Threshold Voltage"] = self.GetThresholdVoltage

        if self.EvolutionController and self.LiquidCooled:
            Line["Utility Pickup Voltage"] = self.GetPickUpVoltage
            Line["Set Output Voltage"] = self.GetSetOutputVoltage

        # Generator time
        Time["Monitor Time"] = datetime.datetime.now().strftime("%A %B %-d, %Y %H:%M:%S")
        Time["Generator Time"] = self.GetDateTime()

        if DictOut:
            ReturnValue = collections.OrderedDict()
            ReturnValue = self.ProcessDispatch(Status, ReturnValue)
        else:
            ReturnValue = self.printToScreen(self.ProcessDispatch(Status,""), ToString)

        return ReturnValue


    #------------ GeneratorDevice::DisplayMaintenance ----------------------------------------
    def DisplayMaintenance (self, ToString = False, DictOut = False):

        if DictOut:
            ToString = True
        # use ordered dict to maintain order of output
        # ordered dict to handle evo vs nexus functions
        Maintenance = collections.OrderedDict()
        Maint = collections.OrderedDict()
        Maintenance["Maintenance"] = Maint
        Maint["Model"] = self.Model
        Maint["Generator Serial Number"] = self.GetSerialNumber
        Maint["Controller"] = self.GetController
        Maint["Nominal RPM"] = self.NominalRPM
        Maint["Rated kW"] = self.NominalKW
        Maint["Nominal Frequency"] = self.NominalFreq
        Maint["Fuel Type"] = self.FuelType
        Exercise = collections.OrderedDict()
        Exercise["Exercise Time"] = self.GetExerciseTime
        if self.EvolutionController and self.LiquidCooled:
            Exercise["Exercise Duration"] = self.GetExerciseDuration
        Maint["Exercise"] = Exercise
        Service = collections.OrderedDict()
        if not self.EvolutionController and self.LiquidCooled:
            Service["Air Filter Service Due"] = self.GetServiceDue("AIR") + " or " + self.GetServiceDueDate("AIR")
            Service["Oil Change and Filter Due"] = self.GetServiceDue("OIL") + " or " + self.GetServiceDueDate("OIL")
            Service["Spark Plug Change Due"] = self.GetServiceDue("SPARK") + " or " + self.GetServiceDueDate("SPARK")
        elif not self.EvolutionController and not self.LiquidCooled:
            # Note: On Nexus AC These represent Air Filter, Oil Filter, and Spark Plugs, possibly 5 all together
            # The labels are generic for now until I get clarification from someone with a Nexus AC
            Service["Air Filter Service Due"] = self.GetServiceDue("AIR")  + " or " + self.GetServiceDueDate("AIR")
            Service["Oil and Oil Filter Service Due"] = self.GetServiceDue("OIL") + " or " + self.GetServiceDueDate("OIL")
            Service["Spark Plug Service Due"] = self.GetServiceDue("SPARK") + " or " + self.GetServiceDueDate("SPARK")
            Service["Battery Service Due"] = self.GetServiceDue("BATTERY") + " or " + self.GetServiceDueDate("BATTERY")
        else:
            Service["Service A Due"] = self.GetServiceDue("A") + " or " + self.GetServiceDueDate("A")
            Service["Service B Due"] = self.GetServiceDue("B") + " or " + self.GetServiceDueDate("B")

        Service["Total Run Hours"] = self.GetRunTimes
        Service["Hardware Version"] = self.GetHardwareVersion
        Service["Firmware Version"] = self.GetFirmwareVersion
        Maint["Service"] = Service

        if DictOut:
            ReturnValue = collections.OrderedDict()
            ReturnValue = self.ProcessDispatch(Maintenance, ReturnValue)
        else:
            ReturnValue = self.printToScreen(self.ProcessDispatch(Maintenance,""), ToString)

        return ReturnValue

    #------------ GeneratorDevice::GetStartInfo ----------------------------------------
    def GetStartInfo(self):

        StartInfo = {}

        StartInfo["sitename"] = self.SiteName
        StartInfo["fueltype"] = self.FuelType
        StartInfo["model"] = self.Model
        StartInfo["nominalKW"] = self.NominalKW
        StartInfo["nominalRPM"] = self.NominalRPM
        StartInfo["nominalfrequency"] = self.NominalFreq
        StartInfo["Controller"] = self.GetController(Actual = False)

        return StartInfo

    #------------ GeneratorDevice::signed16-------------------------------
    def signed16(self, value):
        return -(value & 0x8000) | (value & 0x7fff)
    #------------ GeneratorDevice::RoundInt-------------------------------
    def RoundInt(self, number, roundto):

        rem = number % roundto
        if rem < (roundto/2):
            number = int(number / roundto) * roundto
        else:
            number = int((number + roundto) / roundto) * roundto
        return number

    #------------ GeneratorDevice::DisplayUnknownSensors-------------------------------
    def DisplayUnknownSensors(self):

        Sensors = collections.OrderedDict()

        if not self.bDisplayUnknownSensors:
            return ""

        # Evo Liquid Cooled: ramps up to 300 decimal (1800 RPM)
        # Nexus and Evo Air Cooled: ramps up to 600 decimal on LP/NG   (3600 RPM)
        # this is possibly raw data from RPM sensor
        Value = self.GetUnknownSensor("003c")
        if len(Value):
            Sensors["Raw RPM Sensor"] = Value

            Sensors["Frequency (Calculated)"] = self.GetFrequency(Calculate = True)

        if self.EvolutionController:
            Value = self.GetUnknownSensor("0208")
            if len(Value):
                Sensors["Calibrate Volts Value"] = Value

        if self.EvolutionController and self.LiquidCooled:

            Sensors["Battery Status (Sensor)"] = self.GetBatteryStatusAlternate()

            # get UKS
            Value = self.GetUnknownSensor("05ee")
            if len(Value):
                # Fahrenheit = 9.0/5.0 * Celsius + 32
                FloatTemp = int(Value) / 10.0
                FloatStr = "%2.1f" % FloatTemp
                Sensors["Battery Charger Sensor"] = FloatStr

             # get UKS
            Value = self.GetUnknownSensor("05ed")
            if len(Value):
                import math
                # Fahrenheit = 9.0/5.0 * Celsius + 32
                SensorValue = float(Value)
                #=(SQRT((Q17-$P$15)*$Q$15)+$R$15)*-1
                # 5, 138, -20
                Celsius = math.sqrt(  (SensorValue-10)*125) * -1 - (-88)
                #Celsius = (math.sqrt(  (SensorValue-10)*125) + (-88)) * -1
                # =SQRT(((SensorValue-10)*125))*-1-(-88)
                # V1 = Celsius = (SensorValue - 77.45) * -1.0
                Fahrenheit = 9.0/5.0 * Celsius + 32
                CStr = "%.1f" % Celsius
                FStr = "%.1f" % Fahrenheit
                Sensors["Ambient Temp Thermistor"] = "Sensor: " + Value + ", " + CStr + "C, " + FStr + "F"

            # get total hours since activation
            Value = self.GetRegisterValueFromList("0054")
            if len(Value):
                StrVal = "%d H" % int(Value,16)
                Sensors["Hours of Protection"] = StrVal

        if self.EvolutionController and not self.LiquidCooled:
            Sensors["Output Current"] = self.GetCurrentOutput()
            Sensors["Output Power (Single Phase)"] = self.GetPowerOutput()

            if self.EvolutionController:
                Value = self.GetUnknownSensor("05f6")
                if len(Value):
                    Sensors["Calibrate Current 1 Value"] = Value
                Value = self.GetUnknownSensor("05f7")
                if len(Value):
                    Sensors["Calibrate Current 2 Value"] = Value

        if not self.LiquidCooled:       # Nexus AC and Evo AC

            # starts  0x4000 when idle, ramps up to ~0x2e6a while running
            Value = self.GetUnknownSensor("0032", RequiresRunning = True)
            if len(Value):
                FloatTemp = int(Value) / 100.0
                FloatStr = "%.2f" % FloatTemp
                Sensors["Unsupported Sensor 1"] = FloatStr

            Value = self.GetUnknownSensor("0033")
            if len(Value):
                Sensors["Unsupported Sensor 2"] = Value

            # return -2 thru 2
            Value = self.GetUnknownSensor("0034")
            if len(Value):
                SignedStr = str(self.signed16( int(Value)))
                Sensors["Unsupported Sensor 3"] = SignedStr

            #
            Value = self.GetUnknownSensor("003b")
            if len(Value):
                Sensors["Unsupported Sensor 4"] = Value

        return Sensors

    #------------ GeneratorDevice::LogRange --------------------------------------------
    # used for iterating log registers
    def LogRange(self, start, count, step):
        Counter = 0
        while Counter < count:
            yield start
            start += step
            Counter += 1

    #------------ GeneratorDevice::GetOneLogEntry --------------------------------------------
    def GetOneLogEntry(self, Register, LogBase, RawOutput = False):

        outstring = ""
        RegStr = "%04x" % Register
        Value = self.GetRegisterValueFromList(RegStr)
        if len(Value) == 0:
            return False, ""
        if not RawOutput:
            LogStr = self.ParseLogEntry(Value, LogBase = LogBase)
            if len(LogStr):             # if the register is there but no log entry exist
                outstring += self.printToScreen(LogStr, outstr = True, nonewline = True)
        else:
            outstring += self.printToScreen("%s:%s" % (RegStr, Value), outstr = True, nonewline = True)

        return True, outstring

    #------------ GeneratorDevice::GetLogs --------------------------------------------
    def GetLogs(self, Title, StartReg, Stride, AllLogs = False, RawOutput = False):

        # The output will be a Python Dictionary with a key (Title) and
        # the entry will be a list of strings (or one string if not AllLogs,

        RetValue = collections.OrderedDict()
        LogList = []
        Title = Title.strip()
        Title = Title.replace(":","")

        if AllLogs:
            for Register in self.LogRange(StartReg , LOG_DEPTH, Stride):
                bSuccess, LogEntry = self.GetOneLogEntry(Register, StartReg, RawOutput)
                if not bSuccess or len(LogEntry) == 0:
                    break
                LogList.append(LogEntry)

            RetValue[Title] = LogList
            return RetValue
        else:
            bSuccess, LogEntry = self.GetOneLogEntry(StartReg, StartReg, RawOutput)
            if bSuccess:
                RetValue[Title] = LogEntry
            return RetValue

    #------------ GeneratorDevice::DisplayLogs --------------------------------------------
    def DisplayLogs(self, AllLogs = False, RawOutput = False, ToString = False, DictOut = False):

        # if DictOut is True, return a dictionary with a list of Dictionaries (one for each log)
        # Each dict in the list is a log (alarm, start/stop). For Example:
        #
        #       Dict[Logs] = [ {"Alarm Log" : [Log Entry1, LogEntry2, ...]},
        #                      {"Start Stop Log" : [Log Entry3, Log Entry 4, ...]}...]

        ALARMLOG     = "Alarm Log:     "
        SERVICELOG   = "Service Log:   "
        STARTSTOPLOG = "Start Stop Log:"

        EvolutionLog = [[ALARMLOG, ALARM_LOG_STARTING_REG, ALARM_LOG_STRIDE],
                        [SERVICELOG, SERVICE_LOG_STARTING_REG, SERVICE_LOG_STRIDE],
                        [STARTSTOPLOG, START_LOG_STARTING_REG, START_LOG_STRIDE]]
        NexusLog     = [[ALARMLOG, NEXUS_ALARM_LOG_STARTING_REG, NEXUS_ALARM_LOG_STRIDE],
                        [STARTSTOPLOG, START_LOG_STARTING_REG, START_LOG_STRIDE]]

        LogParams = EvolutionLog if self.EvolutionController else NexusLog

        RetValue = collections.OrderedDict()
        LogList = []

        for Params in LogParams:
            LogOutput = self.GetLogs(Params[0], Params[1], Params[2], AllLogs, RawOutput)
            LogList.append(LogOutput)

        RetValue["Logs"] = LogList

        UnknownFound = False
        List = RetValue.get("Logs", [])
        for Logs in List:
            for Key, Entries in Logs.items():
                if not AllLogs:
                    if "unknown" in Entries.lower():
                        UnknownFound = True
                        break
                else:
                    for LogItems in Entries:
                        if "unknown" in LogItems.lower():
                            UnknownFound = True
                            break
        if UnknownFound:
            msgbody = "\nThe output appears to have unknown values. Please see the following threads to resolve these issues:"
            msgbody += "\n        https://github.com/jgyates/genmon/issues/12"
            msgbody += "\n        https://github.com/jgyates/genmon/issues/13"
            RetValue["Note"] = msgbody
            self.SendFeedbackInfo("Logs", FullLogs = True, Always = True, Message="Unknown Entries in Log")

        if not DictOut:
            return self.printToScreen(self.ProcessDispatch(RetValue,""), ToString)

        return RetValue


    #----------  GeneratorDevice::ParseLogEntry-------------------------------
    #  Log Entries are in one of two formats, 16 (On off Log, Service Log) or
    #   20 chars (Alarm Log)
    #     AABBCCDDEEFFGGHHIIJJ
    #       AA = Log Code - Unique Value for displayable string
    #       BB = log entry number
    #       CC = minutes
    #       DD = hours
    #       EE = Month
    #       FF = Date
    #       GG = year
    #       HH = seconds
    #       IIJJ = Alarm Code for Alarm Log only
    #---------------------------------------------------------------------------
    def ParseLogEntry(self, Value, LogBase = None):
        # This should be the same for all models
        StartLogDecoder = {
        0x28: "Switched Off",               # Start / Stop Log
        0x29: "Running - Manual",           # Start / Stop Log
        0x2A: "Stopped - Auto",             # Start / Stop Log
        0x2B: "Running - Utility Loss",     # Start / Stop Log
        0x2C: "Running - 2 Wire Start",     # Start / Stop Log
        0x2D: "Running - Remote Start",     # Start / Stop Log
        0x2E: "Running - Exercise",         # Start / Stop Log
        0x2F: "Stopped - Alarm"             # Start / Stop Log
        # Stopped Alarm
        }

        # This should be the same for all Evo models , Not sure about service C, this may be a Nexus thing
        ServiceLogDecoder = {
        0x16: "Service Schedule B",         # Maint
        0x17: "Service Schedule A",         # Maint
        0x18: "Inspect Battery",
        0x3C: "Schedule B Serviced",        # Maint
        0x3D: "Schedule A Serviced",        # Maint
        0x3E: "Battery Maintained",
        0x3F: "Maintenance Reset"
        # This is from the diagnostic manual.
        # *Schedule Service A
        # Schedule Service B
        # Schedule Service C
        # *Schedule A Serviced
        # Schedule B Serviced
        # Schedule C Serviced
        # Inspect Battery
        # Maintenance Reset
        # Battery Maintained
        }

        AlarmLogDecoder_EvoLC = {
        0x04: "RPM Sense Loss",             # 1500 Alarm
        0x06: "Low Coolant Level",          # 2720  Alarm
        0x47: "Low Fuel Level",             # 2700A Alarm
        0x1B: "Low Fuel Level",             # 2680W Alarm
        0x46: "Ruptured Tank",              # 2710 Alarm
        0x49: "Hall Calibration Error"      # 2810  Alarm
        # Low Oil Pressure
        # High Engine Temperature
        # Overcrank
        # Overspeed
        # RPM Sensor Loss
        # Underspeed
        # Underfrequency
        # Wiring Error
        # Undervoltage
        # Overvoltage
        # Internal Fault
        # Firmware Error
        # Stepper Overcurrent
        # Fuse Problem
        # Ruptured Basin
        # Canbus Error
        ####Warning Displays
        # Low Battery
        # Maintenance Periods
        # Exercise Error
        # Battery Problem
        # Charger Warning
        # Charger Missing AC
        # Overload Cooldown
        # USB Warning
        # Download Failure
        # FIRMWARE ERROR-9
        }

        # Evolution Air Cooled Decoder
        # NOTE: Warnings on Evolution Air Cooled have an error code of zero
        AlarmLogDecoder_EvoAC = {
        0x13 : "FIRMWARE ERROR-25",
        0x14 : "Low Battery",
        0x15 : "Exercise Set Error",
        0x16 : "Service Schedule B",
        0x17 : "Service Schedule A ",
        0x18 : "Inspect Battery",
        0x19 : "SEEPROM ABUSE",
        0x1c : "Stopping.....",
        0x1d : "FIRMWARE ERROR-9",
        0x1e : "Fuel Pressure",
        0x1f : "Battery Problem",
        0x20 : "Charger Warning",
        0x21 : "Charger Missing AC",
        0x22 : "Overload Warning",
        0x23 : "Overload Cooldown",
        0x25 : "VSCF Warning",
        0x26 : "USB Warning",
        0x27 : "Download Failure",
        0x28 : "High Engine Temp",
        0x29 : "Low Oil Pressure",
        0x2a : "Overcrank",
        0x2b : "Overspeed",
        0x2c : "RPM Sense Loss",
        0x2d : "Underspeed",
        0x2e : "Controller Fault",
        0x2f : "FIRMWARE ERROR-7",
        0x30 : "WIRING ERROR",
        0x31 : "Over Voltage",
        0x32 : "Under Voltage",
        0x33 : "Overload Remove Load",
        0x34 : "Low Volts Remove Load",
        0x35 : "Stepper Over Current",
        0x36 : "Fuse Problem",
        0x39 : "Loss of Speed Signal",
        0x3a : "Loss of Serial Link ",
        0x3b : "VSCF Alarm",
        0x3c : "Schedule B Serviced",
        0x3d : "Schedule A Serviced",
        0x3e : "Battery Maintained",
        0x3f : "Maintenance Reset"
        }

        NexusAlarmLogDecoder = {
        0x00: "High Engine Temperature",    # Validated on Nexus Air Cooled
        0x01: "Low Oil Pressure",           # Validated on Nexus Liquid Cooled
        0x02: "Overcrank",                  # Validated on Nexus Air Cooled
        0x03: "Overspeed",                  # Validated on Nexus Air Cooled
        0x04: "RPM Sense Loss",             # Validated on Nexus Liquid Cooled and Air Cooled
        0x0B: "Low Cooling Fluid",          # Validated on Nexus Liquid Cooled
        0x0C: "Canbus Error",               # Validated on Nexus Liquid Cooled
        0x0F: "Govenor Fault",              # Validated on Nexus Liquid Cooled
        0x14: "Low Battery",                # Validated on Nexus Air Cooled
        0x17: "Inspect Air Filter",         # Validated on Nexus Liquid Cooled
        0x1b: "Check Battery",              # Validated on Nexus Air Cooled
        0x1E: "Low Fuel Pressure",          # Validated on Nexus Liquid Cooled
        0x21: "Service Schedule A",         # Validated on Nexus Liquid Cooled
        0x22: "Service Schedule B"          # Validated on Nexus Liquid Cooled
        }

        # Service Schedule log and Start/Stop Log are 16 chars long
        # error log is 20 chars log
        if len(Value) < 16:
            self.LogError("Error in  ParseLogEntry length check (16)")
            return ""

        if len(Value) > 20:
            self.LogError("Error in  ParseLogEntry length check (20)")
            return ""

        TempVal = Value[8:10]
        Month = int(TempVal, 16)
        if Month == 0 or Month > 12:    # validate month
            # This is the normal return path for an empty log entry
            return ""

        TempVal = Value[4:6]
        Min = int(TempVal, 16)
        if Min >59:                     # validate minute
            self.LogError("Error in  ParseLogEntry minutes check")
            return ""

        TempVal = Value[6:8]
        Hour = int(TempVal, 16)
        if Hour > 23:                   # validate hour
            self.LogError("Error in  ParseLogEntry hours check")
            return ""

        # Seconds
        TempVal = Value[10:12]
        Seconds = int(TempVal, 16)
        if Seconds > 59:
            self.LogError("Error in  ParseLogEntry seconds check")
            return ""

        TempVal = Value[14:16]
        Day = int(TempVal, 16)
        if Day == 0 or Day > 31:        # validate day
            self.LogError("Error in  ParseLogEntry day check")
            return ""

        TempVal = Value[12:14]
        Year = int(TempVal, 16)         # year

        TempVal = Value[0:2]            # this value represents a unique display string
        LogCode = int(TempVal, 16)

        DecoderLookup = {}

        if self.EvolutionController and not self.LiquidCooled:
            DecoderLookup[ALARM_LOG_STARTING_REG] = AlarmLogDecoder_EvoAC
            DecoderLookup[SERVICE_LOG_STARTING_REG] = AlarmLogDecoder_EvoAC
        else:
            DecoderLookup[ALARM_LOG_STARTING_REG] = AlarmLogDecoder_EvoLC
            DecoderLookup[SERVICE_LOG_STARTING_REG] = ServiceLogDecoder

        DecoderLookup[START_LOG_STARTING_REG] = StartLogDecoder
        DecoderLookup[NEXUS_ALARM_LOG_STARTING_REG] = NexusAlarmLogDecoder

        if LogBase == NEXUS_ALARM_LOG_STARTING_REG and self.EvolutionController:
            self.LogError("Error in ParseLog: Invalid Base Register %X", LogBase)
            return "Error Parsing Log Entry"

        Decoder = DecoderLookup.get(LogBase, "Error Parsing Log Entry")

        if isinstance(Decoder, str):
            self.LogError("Error in ParseLog: Invalid Base Register %X", ALARM_LOG_STARTING_REG)
            return Decoder

        # Get the readable string, if we have one
        LogStr = Decoder.get(LogCode, "Unknown 0x%02X" % LogCode)

        # This is a numeric value that increments for each new log entry
        TempVal = Value[2:4]
        EntryNumber = int(TempVal, 16)

        # this will attempt to find a description for the log entry based on the info in ALARMS.txt
        if LogBase == ALARM_LOG_STARTING_REG and "unknown" in LogStr.lower() and  self.EvolutionController and len(Value) > 16:
            TempVal = Value[16:20]      # get alarm code
            AlarmStr = self.GetAlarmInfo(TempVal, ReturnNameOnly = True, FromLog = True)
            if not "unknown" in AlarmStr.lower():
                LogStr = AlarmStr

        RetStr = "%02d/%02d/%02d %02d:%02d:%02d %s " % (Month,Day,Year,Hour,Min, Seconds, LogStr)
        if len(Value) > 16:
            TempVal = Value[16:20]
            AlarmCode = int(TempVal,16)
            RetStr += ": Alarm Code: %04d" % AlarmCode

        return RetStr

    #------------------- GeneratorDevice::GetAlarmInfo -----------------
    # Read file alarm file and get more info on alarm if we have it
    # passes ErrorCode as string of hex values
    def GetAlarmInfo(self, ErrorCode, ReturnNameOnly = False, FromLog = False):

        if not self.EvolutionController:
            return ""
        try:
            # Evolution Air Cooled will give a code of 0000 for warnings
            # Note: last error code can be zero if controller was power cycled
            if ErrorCode == "0000":
                if ReturnNameOnly:
                    # We should not see a zero in the alarm log, this would indicate a true UNKNOWN
                    # returning unknown here is OK since ParseLogEntry will look up a code also
                    return "Warning Code Unknown: %d" % int(ErrorCode,16)
                else:
                    # This can occur if the controller was power cycled and not alarms have occurred since power applied
                    return "Error Code 0000: No alarms occured since controller has been power cycled.\n"

            with open(self.AlarmFile,"r") as AlarmFile:     #opens file

                for line in AlarmFile:
                    line = line.strip()                   # remove newline at beginning / end and trailing whitespace
                    if not len(line):
                        continue
                    if line[0] == "#":              # comment?
                        continue
                    Items = line.split("!")
                    if len(Items) != 5:
                        continue
                    if Items[0] == str(int(ErrorCode,16)):
                        if ReturnNameOnly:
                            outstr = Items[2]
                        else:
                            outstr =  Items[2] + ", Error Code: " + Items[0] + "\n" + "    Description: " + Items[3] + "\n" + "    Additional Info: " + Items[4] + "\n"
                        return outstr

        except Exception as e1:
            self.LogError("Error in  GetAlarmInfo " + str(e1))

        AlarmCode = int(ErrorCode,16)
        return "Error Code Unknown: %04d\n" % AlarmCode

    #------------ GeneratorDevice::GetSerialNumber --------------------------------------
    def GetSerialNumber(self):

        # serial number format:
        # Hex Register Values:  30 30 30 37 37 32 32 39 38 37 -> High part of each byte = 3, low part is SN
        #                       decode as s/n 0007722987
        # at present I am guessing that the 3 that is interleaved in this data is the line of gensets (air cooled may be 03?)
        RegStr = "%04x" % MODEL_REG
        Value = self.GetRegisterValueFromList(RegStr)       # Serial Number Register
        if len(Value) != 20:
            return ""

        if Value[0] == 'f' and Value[1] == 'f':
            # this occurs if the controller has been replaced
            return "None - Controller has been replaced"

        SerialNumberHex = 0x00
        BitPosition = 0
        for Index in range(len(Value) -1 , 0, -1):
            TempVal = Value[Index]
            if (Index & 0x01 == 0):     # only odd positions
                continue

            HexVal = int(TempVal, 16)
            SerialNumberHex = SerialNumberHex | ((HexVal) << (BitPosition))
            BitPosition += 4

        return "%010x" % SerialNumberHex

    #----------  ParseRegisters:GetHardwareVersion  ---------------------------------
    def GetHardwareVersion(self):

        Value = self.GetRegisterValueFromList("002a")
        if len(Value) != 4:
            return ""
        RegVal = int(Value, 16)

        IntTemp = RegVal >> 8           # high byte is firmware version
        FloatTemp = IntTemp / 100.0
        return "V%2.2f" % FloatTemp     #

    #----------  ParseRegisters:GetFirmwareVersion  ---------------------------------
    def GetFirmwareVersion(self):
        Value = self.GetRegisterValueFromList("002a")
        if len(Value) != 4:
            return ""
        RegVal = int(Value, 16)

        IntTemp = RegVal & 0xff         # low byte is firmware version
        FloatTemp = IntTemp / 100.0
        return "V%2.2f" % FloatTemp     #

     #------------ GeneratorDevice::GetTransferStatus --------------------------------------
    def GetTransferStatus(self):

        if not self.EvolutionController:
            return ""                           # Nexus
        else:
            if self.LiquidCooled:               # Evolution
                Register = "0053"
            else:
                return ""

        Value = self.GetRegisterValueFromList(Register)
        if len(Value) != 4:
            return ""
        RegVal = int(Value, 16)

        if self.BitIsEqual(RegVal, 0x01, 0x01):
            return "Generator"
        else:
            return "Utility"


    ##------------ GeneratorDevice::SystemInAlarm --------------------------------------
    def SystemInAlarm(self):

        AlarmState = self.GetAlarmState()

        if len(AlarmState):
            self.GeneratorInAlarm = True
            return True

        self.GeneratorInAlarm = False
        return False

    ##------------ GeneratorDevice::GetAlarmState --------------------------------------
    def GetAlarmState(self):

        strSwitch = self.GetSwitchState()

        if len(strSwitch) == 0:
            return ""

        outString = ""

        Value = self.GetRegisterValueFromList("0001")
        if len(Value) != 8:
            return ""
        RegVal = int(Value, 16)

        if "alarm" in strSwitch.lower() and self.EvolutionController:
            Value = self.GetRegisterValueFromList("05f1")   # get last error code
            if len(Value) == 4:
                AlarmStr = self.GetAlarmInfo(Value, ReturnNameOnly = True)
                if not "unknown" in AlarmStr.lower():
                    outString = AlarmStr

        if "alarm" in strSwitch.lower() and len(outString) == 0:        # is system in alarm/warning
            # These codes indicate an alarm needs to be reset before the generator will run again
            if self.BitIsEqual(RegVal, 0x0FFFF, 0x01):          #  Validate on Nexus, occurred when Low Battery Alarm
                outString += "Low Battery"
            elif self.BitIsEqual(RegVal, 0x0FFFF, 0x08):        #  Validate on Evolution, occurred when forced low coolant
                outString += "Low Coolant"
            elif self.BitIsEqual(RegVal, 0x0FFFF, 0x0d):        #  Validate on Evolution, occurred when forcing RPM sense loss from manual start
                outString += "RPM Sense Loss"
            elif self.BitIsEqual(RegVal, 0x0FFFF, 0x1F):        #  Validate on Evolution, occurred when forced service due
                outString += "Service Due"
            elif self.BitIsEqual(RegVal, 0x0FFFF, 0x20):        #  Validate on Evolution, occurred when service reset
                outString += "Service Complete"
            elif self.BitIsEqual(RegVal, 0x0FFFF, 0x30):        #  Validate on Evolution, occurred when forced ruptured tank
                outString += "Ruptured Tank"
            elif self.BitIsEqual(RegVal, 0x0FFFF, 0x31):        #  Validate on Evolution, occurred when Low Fuel Level
                outString += "Low Fuel Level"
            elif self.BitIsEqual(RegVal, 0x0FFFF, 0x34):        #  Validate on Evolution, occurred when E-Stop
                outString += "Emergency Stop"
            elif self.BitIsEqual(RegVal, 0x0FFFF, 0x14):        #  Validate on Nexus, occurred when Check Battery Alarm
                outString += "Check Battery"
            else:
                self.SendFeedbackInfo("Alarm", Always = True, Message = "Reg 0001 = %08x" % RegVal)
                outString += "UNKNOWN ALARM: %08x" % RegVal

        return outString

    #------------ GeneratorDevice::GetDigitalValues --------------------------------------
    def GetDigitalValues(self, RegVal, LookUp):

        outvalue = ""
        counter = 0x01

        for BitMask, Items in LookUp.items():
            if len(Items[1]):
                if self.BitIsEqual(RegVal, BitMask, BitMask):
                    if Items[0]:
                        outvalue += "%s, " % Items[1]
                else:
                    if not Items[0]:
                        outvalue += "%s, " % Items[1]
        # take of the last comma
        ret = outvalue.rsplit(",", 1)
        return ret[0]

    ##------------ GeneratorDevice::GetSensorInputs --------------------------------------
    def GetSensorInputs(self):

        # at the moment this has only been validated on an Evolution Liquid cooled generator
        # so we will disallow any others from this status
        if not self.EvolutionController:
            return ""        # Nexus

        if not self.LiquidCooled:
            return ""

        # Dict format { bit position : [ Polarity, Label]}
        # Air cooled
        DealerInputs_Evo_AC = { 0x0001: [True, "Manual"],         # Bits 0 and 1 are only momentary (i.e. only set if the button is being pushed)
                                0x0002: [True, "Auto"],           # Bits 0 and 1 are only set in the controller Dealer Test Menu
                                0x0008: [True, "Wiring Error"],
                                0x0020: [True, "High Temperature"],
                                0x0040: [True, "Low Oil Pressure"]}

        DealerInputs_Evo_LC = {
                                0x0001: [True, "Manual Button"],    # Bits 0, 1 and 2 are momentary and only set in the controller
                                0x0002: [True, "Auto Button"],      #  Dealer Test Menu, not in this register
                                0x0004: [True, "Off Button"],
                                0x0008: [True, "2 Wire Start"],
                                0x0010: [True, "Wiring Error"],
                                0x0020: [True, "Ruptured Basin"],
                                0x0040: [False, "E-Stop Activated"],
                                0x0080: [True, "Oil below 8 psi"],
                                0x0100: [True, "Low Coolant"],
                                #0x0200: [False, "Fuel below 5 inch"]}          # Propane/NG
                                0x0200: [True, "Fuel Pressure / Level Low"]}     # Gasoline / Diesel

        if not "diesel" in self.FuelType.lower():
            DealerInputs_Evo_LC[0x0200] = [False, "Fuel below 5 inch"]

        # Nexus Liquid Cooled
        #   Position    Digital inputs      Digital Outputs
        #   1           Low Oil Pressure    air/Fuel Relay
        #   2           Not used            Bosch Enable
        #   3           Low Coolant Level   alarm Relay
        #   4           Low Fuel Pressure   Battery Charge Relay
        #   5           Wiring Error        Fuel Relay
        #   6           two Wire Start      Starter Relay
        #   7           auto Position       Cold Start Relay
        #   8           Manual Position     transfer Relay

        # Nexus Air Cooled
        #   Position    Digital Inputs      Digital Outputs
        #   1           Not Used            Not Used
        #   2           Low Oil Pressure    Not Used
        #   3           High Temperature    Not Used
        #   4           Not Used            Battery Charger Relay
        #   5           Wiring Error Detect Fuel
        #   6           Not Used            Starter
        #   7           Auto                Ignition
        #   8           Manual              Transfer

        # get the inputs registes
        Value = self.GetRegisterValueFromList("0052")
        if len(Value) != 4:
            return ""

        RegVal = int(Value, 16)

        if self.LiquidCooled:
            return self.GetDigitalValues(RegVal, DealerInputs_Evo_LC)
        else:
            return self.GetDigitalValues(RegVal, DealerInputs_Evo_AC)

    #------------ GeneratorDevice::GetDigitalOutputs --------------------------------------
    def GetDigitalOutputs(self):

        if not self.EvolutionController:
            return ""        # Nexus

        if not self.LiquidCooled:
            return ""

        # Dict format { bit position : [ Polarity, Label]}
        # Liquid cooled
        DigitalOutputs_LC = {   0x01: [True, "Transfer Switch Activated"],
                                0x02: [True, "Fuel Enrichment On"],
                                0x04: [True, "Starter On"],
                                0x08: [True, "Fuel Relay On"],
                                0x10: [True, "Battery Charger On"],
                                0x20: [True, "Alarm Active"],
                                0x40: [True, "Bosch Governor On"],
                                0x80: [True, "Air/Fuel Relay On"]}
        # Air cooled
        DigitalOutputs_AC = {   #0x10: [True, "Transfer Switch Activated"],  # Bit Position in Display 0x01
                                0x01: [True, "Ignition On"],                # Bit Position in Display 0x02
                                0x02: [True, "Starter On"],                 # Bit Position in Display 0x04
                                0x04: [True, "Fuel Relay On"],              # Bit Position in Display 0x08
                                #0x08: [True, "Battery Charger On"]         # Bit Position in Display 0x10
                                }

        Register = "0053"

        Value = self.GetRegisterValueFromList(Register)
        if len(Value) != 4:
            return ""
        RegVal = int(Value, 16)

        return self.GetDigitalValues(RegVal, DigitalOutputs_LC)

    #------------ GeneratorDevice::GetEngineState --------------------------------------
    def GetEngineState(self, Reg0001Value = None):

        if Reg0001Value is None:
            Value = self.GetRegisterValueFromList("0001")
            if len(Value) != 8:
                return ""
            RegVal = int(Value, 16)
        else:
            RegVal = Reg0001Value


        # other values that are possible:
        # Running in Warning
        # Running in Alarm
        # Running Remote Start
        # Running Two Wire Start
        # Stopped Alarm
        # Stopped Warning
        # Cranking
        # Cranking Warning
        # Cranking Alarm
        if self.BitIsEqual(RegVal,   0x000F0000, 0x00040000):
            return "Exercising"
        elif self.BitIsEqual(RegVal, 0x000F0000, 0x00090000):
            return "Stopped"
        # Note: this appears to define the state where the generator should start, it defines
        # the initiation of the start delay timer, This only appears in Nexus and Air Cooled Evo
        elif self.BitIsEqual(RegVal, 0x000F0000, 0x00010000):
                return "Startup Delay Timer Activated"
        elif self.BitIsEqual(RegVal, 0x000F0000, 0x00020000):
            if self.SystemInAlarm():
                return "Cranking in Alarm"
            else:
                return "Cranking"
        elif self.BitIsEqual(RegVal, 0x000F0000, 0x00050000):
            return "Cooling Down"
        elif self.BitIsEqual(RegVal, 0x000F0000, 0x00030000):
            if self.SystemInAlarm():
                return "Running in Alarm"
            else:
                return "Running"
        elif self.BitIsEqual(RegVal, 0x000F0000, 0x00060000):
            return "Running in Warning"
        elif self.BitIsEqual(RegVal, 0x000F0000, 0x00080000):
            return "Stopped in Alarm"
        elif self.BitIsEqual(RegVal, 0x000F0000, 0x00000000):
            return "Off - Ready"
        else:
            self.SendFeedbackInfo("EngineState", Always = True, Message = "Reg 0001 = %08x" % RegVal)
            return "UNKNOWN: %08x" % RegVal

    #------------ GeneratorDevice::GetSwitchState --------------------------------------
    def GetSwitchState(self):

        Value = self.GetRegisterValueFromList("0001")
        if len(Value) != 8:
            return ""
        RegVal = int(Value, 16)

        if self.BitIsEqual(RegVal, 0x0FFFF, 0x00):
            return "Auto"
        elif self.BitIsEqual(RegVal, 0x0FFFF, 0x07):
            return "Off"
        elif self.BitIsEqual(RegVal, 0x0FFFF, 0x06):
            return "Manual"
        elif self.BitIsEqual(RegVal, 0x0FFFF, 0x17):
            # This occurs momentarily when stopping via two wire method
            return "Two Wire Stop"
        else:
            return "System in Alarm"

    #------------ GeneratorDevice::GetDateTime -----------------------------------------
    def GetDateTime(self):

        #Generator Time Hi byte = hours, Lo byte = min
        Value = self.GetRegisterValueFromList("000e")
        if len(Value) != 4:
            return ""
        Hour = Value[:2]
        if int(Hour,16) > 23:
            return ""
        Minute = Value[2:]
        if int(Minute,16) >= 60:
            return ""
        # Hi byte = month, Lo byte = day of the month
        Value = self.GetRegisterValueFromList("000f")
        if len(Value) != 4:
            return ""
        Month = Value[:2]
        if int(Month,16) == 0 or int(Month,16) > 12:            # 1 - 12
            return ""
        DayOfMonth = Value[2:]
        if int(DayOfMonth,16) > 31 or int(DayOfMonth,16) == 0:  # 1 - 31
            return ""
        # Hi byte Day of Week 00=Sunday 01=Monday, Lo byte = last 2 digits of year
        Value = self.GetRegisterValueFromList("0010")
        if len(Value) != 4:
            return ""
        DayOfWeek = Value[:2]
        if int(DayOfWeek,16) > 7:
            return ""
        Year = Value[2:]
        if int(Year,16) < 16:
            return ""

        FullDate =self.DaysOfWeek.get(int(DayOfWeek,16),"INVALID") + " " + self.MonthsOfYear.get(int(Month,16),"INVALID")
        FullDate += " " + str(int(DayOfMonth,16)) + ", " + "20" + str(int(Year,16)) + " "
        FullDate += "%02d:%02d" %  (int(Hour,16), int(Minute,16))

        return FullDate

    #------------ GeneratorDevice::GetExerciseDuration --------------------------------------------
    def GetExerciseDuration(self):

        if not self.EvolutionController:
            return ""                       # Not supported on Nexus
        if not self.LiquidCooled:
            return ""                       # Not supported on Air Cooled
        # get exercise time of day
        Value = self.GetRegisterValueFromList("023e")
        if len(Value) != 4:
            return ""
        return "%d min" % int(Value,16)

    #------------ GeneratorDevice::GetParsedExerciseTime --------------------------------------------
    def GetParsedExerciseTime(self):

        retstr = self.GetExerciseTime()
        if not len(retstr):
            return ""
        #should return this format:
        # "Weekly Saturday 13:30 Quiet Mode On"
        # "Biweekly Saturday 13:30 Quiet Mode On"
        # "Monthly Day-1 13:30 Quiet Mode On"
        Items = retstr.split(" ")
        HoursMin = Items[2].split(":")

        if self.bEnhancedExerciseFrequency:
            ModeStr = "True"
        else:
            ModeStr = "False"

        if "monthly" in retstr.lower():
            Items[1] = ''.join(x for x in Items[1] if x.isdigit())
            Day = int(Items[1])
            Items[1] = "%02d" % Day

        retstr = Items[1] + "!" + HoursMin[0] + "!" + HoursMin[1] + "!" + Items[5] + "!" + Items[0] + "!" + ModeStr
        return retstr

    #------------ GeneratorDevice::GetExerciseTime --------------------------------------------
    def GetExerciseTime(self):

        ExerciseFreq = ""   # Weekly
        FreqVal = 0
        DayOfMonth = 0

        if self.bEnhancedExerciseFrequency:
            # get frequency:  00 = weekly, 01= biweekly, 02=monthly
            Value = self.GetRegisterValueFromList("002d")
            if len(Value) != 4:
                return ""

            FreqValStr = Value[2:]
            FreqVal = int(FreqValStr,16)
            if FreqVal > 2:
                return ""

        # get exercise time of day
        Value = self.GetRegisterValueFromList("0005")
        if len(Value) != 4:
            return ""
        Hour = Value[:2]
        if int(Hour,16) > 23:
            return ""
        Minute = Value[2:]
        if int(Minute,16) >= 60:
            return ""

        # Get exercise day of week
        Value = self.GetRegisterValueFromList("0006")
        if len(Value) != 4:
            return ""

        if FreqVal == 0 or FreqVal == 1:        # weekly or biweekly

            DayOfWeek = Value[:2]       # Mon = 1
            if int(DayOfWeek,16) > 7:
                return ""
        elif FreqVal == 2:                      # Monthly
            # Get exercise day of month
            AltValue = self.GetRegisterValueFromList("002e")
            if len(AltValue) != 4:
                return ""
            DayOfMonth = AltValue[2:]
            if int(DayOfMonth,16) > 28:
                return ""

        Type = Value[2:]    # Quiet Mode 00=no 01=yes

        ExerciseTime = ""
        if FreqVal == 0:
            ExerciseTime += "Weekly "
        elif FreqVal == 1:
            ExerciseTime += "Biweekly "
        elif FreqVal == 2:
            ExerciseTime += "Monthly "

        if FreqVal == 0 or FreqVal == 1:
            ExerciseTime +=  self.DaysOfWeek.get(int(DayOfWeek,16),"") + " "
        elif FreqVal == 2:
            ExerciseTime +=  ("Day-%d" % (int(DayOfMonth,16))) + " "

        ExerciseTime += "%02d:%02d" %  (int(Hour,16), int(Minute,16))

        if Type == "00":
            ExerciseTime += " Quiet Mode Off"
        elif Type == "01":
            ExerciseTime += " Quiet Mode On"
        else:
            ExerciseTime += " Quiet Mode Unknown"

        return ExerciseTime

    #------------ GeneratorDevice::GetUnknownSensor1-------------------------------------
    def GetUnknownSensor(self, Register, RequiresRunning = False, Hex = False):

        if not len(Register):
            return ""

        if RequiresRunning:
            EngineState = self.GetEngineState()
            # report null if engine is not running
            if "Stopped" in EngineState or "Off" in EngineState or not len(EngineState):
                return "0"

        # get value
        Value = self.GetRegisterValueFromList(Register)
        if len(Value) != 4:
            return ""

        IntTemp = int(Value,16)
        if not Hex:
            SensorValue = "%d" % IntTemp
        else:
            SensorValue = "%x" % IntTemp

        return SensorValue

    #------------ GeneratorDevice::"GetRPM" --------------------------------------------
    def GetRPM(self):

        # get RPM
        Value = self.GetRegisterValueFromList("0007")
        if len(Value) != 4:
            return ""

        RPMValue = "%5d" % int(Value,16)
        return RPMValue

    #------------ GeneratorDevice::GetCurrentOutput ---------------------------------------
    def GetCurrentOutput(self):

        if not self.EvolutionController:
            return "0.00A"

        EngineState = self.GetEngineState()
        # report null if engine is not running
        if "Stopped" in EngineState or "Off" in EngineState or not len(EngineState):
            return "0.00A"

        CurrentFloat = 0.0
        if self.EvolutionController and self.LiquidCooled:
            Value = self.GetRegisterValueFromList("0058")
            if len(Value):
                CurrentFloat = int(Value,16)
                CurrentFloat = max((CurrentFloat * .2248) - 303.268, 0)

        elif self.EvolutionController and not self.LiquidCooled:
            E1Current = 0
            E2Current = 0

            Value = self.GetRegisterValueFromList("05f4")
            if len(Value):
                E1Current = int(Value,16)
            Value = self.GetRegisterValueFromList("05f5")
            if len(Value):
                E2Current = int(Value,16)
            CurrentFloat = float(E1Current + E2Current)
            #Value = self.GetRegisterValueFromList("003B")
            #if len(Value):
            #    CurrentFloat = int(Value,16)
            #    CurrentFloat = max((CurrentFloat * 0.398) - 233.8, 0)

            #    # =max(E7 * 0.398) - 223.8),0)
            #    #CurrentFloat = max((CurrentFloat * 0.0689) - 291.6, 0)

        return "%.2fA" % CurrentFloat

     ##------------ GeneratorDevice::GetActiveRotorPoles ---------------------------------------
    def GetActiveRotorPoles(self):
        # (2 * 60 * Freq) / RPM = Num Rotor Poles

        if not self.EvolutionController:
            return ""

        FreqStr = self.removeAlpha(self.GetFrequency())
        RPMStr = self.removeAlpha(self.GetRPM().strip())

        RotorPoles = "0"
        if len(FreqStr) and len(RPMStr):
            RPMInt = int(RPMStr)
            if RPMInt:
                FreqFloat = float(FreqStr)
                NumRotorPoles = int(round((2 * 60 * FreqFloat) / RPMInt))
                if NumRotorPoles > 4:
                    NumRotorPoles = 0
                RotorPoles = str(NumRotorPoles)

        return RotorPoles


    #------------ GeneratorDevice::PrunePowerLog-------------------------
    def PrunePowerLog(self, Minutes):

        if not Minutes:
            return self.ClearPowerLog()

        try:
            CmdString = "power_log_json=%d" % Minutes
            PowerLog = self.GetPowerHistory(CmdString, NoReduce = True)

            LogSize = os.path.getsize(self.PowerLog)
            self.ClearPowerLog()

            # is the file size too big?
            if LogSize / (1024*1024) >= self.PowerLogMaxSize:
                return "OK"

            if LogSize / (1024*1024) >= self.PowerLogMaxSize * 0.8:
                msgbody = "The kwlog file size is 80% of the maximum. Once the log reaches 100% of the maximum size the log will be reset."
                self.mail.sendEmail("Notice: Log file size warning" , msgbody, msgtype = "warn")

            # Write oldest log entries first
            for Items in reversed(PowerLog):
                self.LogToFile(self.PowerLog, Items[0], Items[1])

            LogSize = os.path.getsize(self.PowerLog)
            if LogSize == 0:
                TimeStamp = datetime.datetime.now().strftime('%x %X')
                self.LogToFile(self.PowerLog, TimeStamp, "0.0")

            return "OK"

        except Exception as e1:
            self.LogError("Error in  ClearPowerLog: " + str(e1))
            return "Error in  ClearPowerLog: " + str(e1)

    #------------ GeneratorDevice::ClearPowerLog-------------------------
    def ClearPowerLog(self):

        try:
            if not len(self.PowerLog):
                return "Power Log Disabled"

            if not os.path.isfile(self.PowerLog):
                return "Power Log is empty"
            os.remove(self.PowerLog)

            # add zero entry to note the start of the log
            TimeStamp = datetime.datetime.now().strftime('%x %X')
            self.LogToFile(self.PowerLog, TimeStamp, "0.0")

            return "Power Log cleared"
        except Exception as e1:
            self.LogError("Error in  ClearPowerLog: " + str(e1))
            return "Error in  ClearPowerLog: " + str(e1)

    #------------ GeneratorDevice::ReducePowerSamples-------------------------
    def ReducePowerSamplesOld(self, PowerList, MaxSize):

        if MaxSize == 0:
            self.LogError("RecducePowerSamples: Error: Max size is zero")
            return []

        if len(PowerList) < MaxSize:
            self.LogError("RecducePowerSamples: Error: Can't reduce ")
            return PowerList

        try:
            Sample = int(len(PowerList) / MaxSize)
            Remain = int(len(PowerList) % MaxSize)

            NewList = []
            Count = 0
            for Count in range(len(PowerList)):
                TimeStamp, KWValue = PowerList[Count]
                if float(KWValue) == 0:
                        NewList.append([TimeStamp,KWValue])
                elif ( Count % Sample == 0 ):
                    NewList.append([TimeStamp,KWValue])

            # if we have too many entries due to a remainder or not removing zero samples, then delete some
            if len(NewList) > MaxSize:
                return RemovePowerSamples(NewList, MaxSize)
        except Exception as e1:
            self.LogError("Error in RecducePowerSamples: %s" % str(e1))
            return PowerList

        return NewList

    #------------ GeneratorDevice::RemovePowerSamples-------------------------
    def RemovePowerSamples(List, MaxSize):

        try:
            if len(List) <= MaxSize:
                self.LogError("RemovePowerSamples: Error: Can't remove ")
                return List

            Extra = len(List) - MaxSize
            for Count in range(Extra):
                    # assume first and last sampels are zero samples so don't select thoes
                    self.MarkNonZeroKwEntry(List, random.randint(1, len(List) - 2))

            TempList = []
            for TimeStamp, KWValue in List:
                if not TimeStamp == "X":
                    TempList.append([TimeStamp, KWValue])
            return TempList
        except Exception as e1:
            self.LogError("Error in RemovePowerSamples: %s" % str(e1))
            return List

    #------------ GeneratorDevice::MarkNonZeroKwEntry-------------------------
    #       RECURSIVE
    def MarkNonZeroKwEntry(self, List, Index):

        try:
            TimeStamp, KwValue = List[Index]
            if not KwValue == "X" and not float(KwValue) == 0.0:
                List[Index] = ["X", "X"]
                return
            else:
                MarkNonZeroKwEntry(List, Index - 1)
                return
        except Exception as e1:
            self.LogError("Error in MarkNonZeroKwEntry: %s" % str(e1))
        return

    #------------ GeneratorDevice::ReducePowerSamples-------------------------
    def ReducePowerSamples(self, PowerList, MaxSize):

        if MaxSize == 0:
            self.LogError("RecducePowerSamples: Error: Max size is zero")
            return []

        periodMaxSamples = MaxSize
        NewList = []
        try:
            CurrentTime = datetime.datetime.now()
            secondPerSample = 0
            prevMax = 0
            currMax = 0
            currTime = CurrentTime
            prevTime = CurrentTime + datetime.timedelta(minutes=1)
            currSampleTime = CurrentTime
            prevBucketTime = CurrentTime # prevent a 0 to be written the first time
            nextBucketTime = CurrentTime - datetime.timedelta(seconds=1)

            for Count in range(len(PowerList)):
               TimeStamp, KWValue = PowerList[Count]
               struct_time = time.strptime(TimeStamp, "%x %X")
               delta_sec = (CurrentTime - datetime.datetime.fromtimestamp(time.mktime(struct_time))).total_seconds()
               if 0 <= delta_sec <= datetime.timedelta(minutes=60).total_seconds():
                   secondPerSample = int(datetime.timedelta(minutes=60).total_seconds() / periodMaxSamples)
               if datetime.timedelta(minutes=60).total_seconds() <= delta_sec <=  datetime.timedelta(hours=24).total_seconds():
                   secondPerSample = int(datetime.timedelta(hours=23).total_seconds() / periodMaxSamples)
               if datetime.timedelta(hours=24).total_seconds() <= delta_sec <= datetime.timedelta(days=7).total_seconds():
                   secondPerSample = int(datetime.timedelta(days=6).total_seconds() / periodMaxSamples)
               if datetime.timedelta(days=7).total_seconds() <= delta_sec <= datetime.timedelta(days=31).total_seconds():
                   secondPerSample = int(datetime.timedelta(days=25).total_seconds() / periodMaxSamples)

               currSampleTime = CurrentTime - datetime.timedelta(seconds=(int(delta_sec / secondPerSample)*secondPerSample))
               if (currSampleTime != currTime):
                   if ((currMax > 0) and (prevBucketTime != prevTime)):
                       NewList.append([prevBucketTime.strftime('%x %X'), 0.0])
                   if ((currMax > 0) or ((currMax == 0) and (prevMax > 0))):
                       NewList.append([currTime.strftime('%x %X'), currMax])
                   if ((currMax > 0) and (nextBucketTime != currSampleTime)):
                       NewList.append([nextBucketTime.strftime('%x %X'), 0.0])
                   prevMax = currMax
                   prevTime = currTime
                   currMax = KWValue
                   currTime = currSampleTime
                   prevBucketTime  = CurrentTime - datetime.timedelta(seconds=((int(delta_sec / secondPerSample)+1)*secondPerSample))
                   nextBucketTime  = CurrentTime - datetime.timedelta(seconds=((int(delta_sec / secondPerSample)-1)*secondPerSample))
               else:
                   currMax = max(currMax, KWValue)


            NewList.append([currTime.strftime('%x %X'), currMax])
        except Exception as e1:
            self.LogError("Error in RecducePowerSamples: %s" % str(e1))
            return PowerList

        return NewList

    #------------ GeneratorDevice::-------------------------
    def GetPowerHistory(self, CmdString, NoReduce = False):

        KWHours = False
        msgbody = "Invalid command syntax for command power_log_json"

        try:
            if not len(self.PowerLog):
                # power log disabled
                return []

            if not len(CmdString):
                self.LogError("Error in GetPowerHistory: Invalid input")
                return []

            #Format we are looking for is "power_log_json=5" or "power_log_json" or "power_log_json=1000,kw"
            CmdList = CmdString.split("=")

            if len(CmdList) > 2:
                self.LogError("Validation Error: Error parsing command string in GetPowerHistory (parse): " + CmdString)
                return msgbody

            CmdList[0] = CmdList[0].strip()

            if not CmdList[0].lower() == "power_log_json":
                self.LogError("Validation Error: Error parsing command string in GetPowerHistory (parse2): " + CmdString)
                return msgbody

            if len(CmdList) == 2:
                ParseList = CmdList[1].split(",")
                if len(ParseList) == 1:
                    Minutes = int(CmdList[1].strip())
                elif len(ParseList) == 2:
                    Minutes = int(ParseList[0].strip())
                    if ParseList[1].strip().lower() == "kw":
                        KWHours = True
                else:
                    self.LogError("Validation Error: Error parsing command string in GetPowerHistory (parse3): " + CmdString)
                    return msgbody

            else:
                Minutes = 0
        except Exception as e1:
            self.LogError("Error in  GetPowerHistory (Parse): %s : %s" % (CmdString,str(e1)))
            return msgbody

        try:
            # check to see if a log file exist yet
            if not os.path.isfile(self.PowerLog):
                return []

            PowerList = []

            with open(self.PowerLog,"r") as LogFile:     #opens file
                CurrentTime = datetime.datetime.now()
                try:
                    for line in LogFile:
                        line = line.strip()                  # remove whitespace at beginning and end

                        if not len(line):
                            continue
                        if line[0] == "#":                  # comment
                            continue
                        Items = line.split(",")
                        if len(Items) != 2:
                            continue

                        if Minutes:
                            struct_time = time.strptime(Items[0], "%x %X")
                            LogEntryTime = datetime.datetime.fromtimestamp(time.mktime(struct_time))
                            Delta = CurrentTime - LogEntryTime
                            if self.GetDeltaTimeMinutes(Delta) < Minutes :
                                PowerList.insert(0, [Items[0], Items[1]])
                        else:
                            PowerList.insert(0, [Items[0], Items[1]])
                    #Shorten list to 1000 if specific duration requested
                    if not KWHours and len(PowerList) > 500 and Minutes and not NoReduce:
                        PowerList = self.ReducePowerSamples(PowerList, 500)
                except Exception as e1:
                    self.LogError("Error in  GetPowerHistory (parse file): " + str(e1))
                    # continue to the next line

            if KWHours:
                TotalTime = datetime.timedelta(seconds=0)
                TotalPower = 0
                LastTime = None
                for Items in PowerList:
                    Power = float(Items[1])
                    struct_time = time.strptime(Items[0], "%x %X")
                    LogEntryTime = datetime.datetime.fromtimestamp(time.mktime(struct_time))
                    if LastTime == None or Power == 0:
                        TotalTime += LogEntryTime - LogEntryTime
                    else:
                        TotalTime += LastTime - LogEntryTime
                    LastTime = LogEntryTime

                    TotalPower += Power
                # return KW Hours
                return "%.2f" % ((TotalTime.total_seconds() / 3600) * TotalPower)

            return PowerList

        except Exception as e1:
            self.LogError("Error in  GetPowerHistory: " + str(e1))
            msgbody = "Error in  GetPowerHistory: " + str(e1)
            return msgbody

    #----------  GeneratorDevice::PowerMeter-------------------------------------
    #----------  Monitors Power Output
    def PowerMeter(self):

        if not len(self.PowerLog):
            self.LogError("Power Log Disabled")
            self.KillThread("PowerMeter", CleanupSelf = True)
            return

        # make sure system is up and running otherwise we will not know which controller is present
        while True:
            time.sleep(1)
            if self.InitComplete:
                break
            if self.IsStopSignaled("PowerMeter"):
                return

        if not self.EvolutionController:    # Not supported by Nexus at this time
            self.KillThread("PowerMeter", CleanupSelf = True)
            return

        # only support EvoAC is Unsupported Sensors is enabled
        if not self.LiquidCooled and not self.bDisplayUnknownSensors:
            self.KillThread("PowerMeter", CleanupSelf = True)
            return

        self.LogError("Power Log Started")
        # if log file is empty or does not exist, make a zero entry in log to denote start of collection
        if not os.path.isfile(self.PowerLog) or os.path.getsize(self.PowerLog) == 0:
            TimeStamp = datetime.datetime.now().strftime('%x %X')
            self.LogToFile(self.PowerLog, TimeStamp, "0.0")

        LastValue = 0.0
        LastPruneTime = datetime.datetime.now()
        while True:
            try:
                time.sleep(5)

                # Housekeeping on kw Log
                if self.GetDeltaTimeMinutes(datetime.datetime.now() - LastPruneTime) > 1440 :     # check every day
                    self.PrunePowerLog(43800)   # delete log entries greater than one month
                    LastPruneTime = datetime.datetime.now()

                # Time to exit?
                if self.IsStopSignaled("PowerMeter"):
                    return
                KWOut = self.removeAlpha(self.GetPowerOutput())
                KWFloat = float(KWOut)

                if LastValue == KWFloat:
                    continue

                if LastValue == 0:
                    StartTime = datetime.datetime.now() - datetime.timedelta(seconds=1)
                    TimeStamp = StartTime.strftime('%x %X')
                    self.LogToFile(self.PowerLog, TimeStamp, str(LastValue))

                LastValue = KWFloat
                # Log to file
                TimeStamp = datetime.datetime.now().strftime('%x %X')
                self.LogToFile(self.PowerLog, TimeStamp, str(KWFloat))

            except Exception as e1:
                self.LogError("Error in PowerMeter: " + str(e1))


    #------------ GeneratorDevice::GetPowerOutput ---------------------------------------
    def GetPowerOutput(self):

        if not self.EvolutionController:
            return ""

        EngineState = self.GetEngineState()
        # report null if engine is not running
        if "Stopped" in EngineState or "Off" in EngineState or not len(EngineState):
            return "0kW"

        CurrentStr = self.removeAlpha(self.GetCurrentOutput())
        VoltageStr = self.removeAlpha(self.GetVoltageOutput())

        PowerOut = 0.0

        if len(CurrentStr) and len(VoltageStr):
            PowerOut = float(VoltageStr) * float(CurrentStr)

        return "%.2fkW" % (PowerOut / 1000.0)


    #------------ GeneratorDevice::GetFrequency ---------------------------------------
    def GetFrequency(self, Calculate = False):

        # get Frequency
        FloatTemp = 0.0

        if not Calculate:
            Value = self.GetRegisterValueFromList("0008")
            if len(Value) != 4:
                return ""

            IntTemp = int(Value,16)
            if self.EvolutionController and self.LiquidCooled:
                FloatTemp = IntTemp / 10.0      # Evolution
            elif not self.EvolutionController and self.LiquidCooled:
                FloatTemp = IntTemp / 1.0       # Nexus Liquid Cooled
                FloatTemp = FloatTemp * 2.0
            else:
                FloatTemp = IntTemp / 1.0       # Nexus and Evolution Air Cooled

        else:
            # (RPM * Poles) / 2 * 60
            RPM = self.GetRPM()
            Poles = self.GetActiveRotorPoles()
            if len(RPM) and len(Poles):
                FloatTemp = (float(RPM) * float(Poles)) / (2*60)

        FreqValue = "%2.1f Hz" % FloatTemp
        return FreqValue

    #------------ GeneratorDevice::GetVoltageOutput --------------------------
    def GetVoltageOutput(self):

        # get Output Voltage
        Value = self.GetRegisterValueFromList("0012")
        if len(Value) != 4:
            return ""

        VolatageValue = "%dV" % int(Value,16)

        return VolatageValue

    #------------ GeneratorDevice::GetPickUpVoltage --------------------------
    def GetPickUpVoltage(self):

         # get Utility Voltage Pickup Voltage
        Value = self.GetRegisterValueFromList("023b")
        if len(Value) != 4:
            return ""
        PickupVoltage = int(Value,16)

        return "%dV" % PickupVoltage

    #------------ GeneratorDevice::GetThresholdVoltage --------------------------
    def GetThresholdVoltage(self):

        # get Utility Voltage Threshold
        Value = self.GetRegisterValueFromList("0011")
        if len(Value) != 4:
            return ""
        ThresholdVoltage = int(Value,16)

        return "%dV" % ThresholdVoltage

    #------------ GeneratorDevice::GetSetOutputVoltage --------------------------
    def GetSetOutputVoltage(self):

        # get set output voltage
        if not self.EvolutionController or not self.LiquidCooled:
            return ""
        Value = self.GetRegisterValueFromList("0237")
        if len(Value) != 4:
            return ""
        SetOutputVoltage = int(Value,16)

        return "%dV" % SetOutputVoltage

    #------------ GeneratorDevice::GetStartupDelay --------------------------
    def GetStartupDelay(self):

        # get Startup Delay
        StartupDelay = 0
        Value = ""
        if self.EvolutionController and not self.LiquidCooled:
            Value = self.GetRegisterValueFromList("002b")
        elif self.EvolutionController and self.LiquidCooled:
            Value = self.GetRegisterValueFromList("0239")
        else:
            return ""
        if len(Value) != 4:
            return ""
        StartupDelay = int(Value,16)

        return "%d s" % StartupDelay

    #------------ GeneratorDevice::GetUtilityVoltage --------------------------
    def GetUtilityVoltage(self):

        # get Utility Voltage
        Value = self.GetRegisterValueFromList("0009")
        if len(Value) != 4:
            return ""

        VolatageValue = "%dV" % int(Value,16)

        return VolatageValue

    #------------ GeneratorDevice::GetBatteryVoltage -------------------------
    def GetBatteryVoltage(self):

        # get Battery Charging Voltage
        Value = self.GetRegisterValueFromList("000a")
        if len(Value) != 4:
            return ""

        IntTemp = int(Value,16)
        FloatTemp = IntTemp / 10.0
        VoltageValue = "%2.1fV" % FloatTemp

        return VoltageValue

    #------------ GeneratorDevice::GetBatteryStatusAlternate -------------------------
    def GetBatteryStatusAlternate(self):

        if not self.EvolutionController:
            return "Not Available"     # Nexus

        EngineState = self.GetEngineState()
        if  not len(EngineState):
            return "Not Charging"
        if not "Stopped" in EngineState and not "Off" in EngineState:
            return "Not Charging"

        Value = self.GetRegisterValueFromList("05ee")
        if len(Value):
            FloatTemp = int(Value,16) / 10.0
            if self.LiquidCooled:
                CompValue = 5.0
            else:
                CompValue = 0
            if FloatTemp > CompValue:
                return "Charging"
            else:
                return "Not Charging"
        return ""

    #------------ GeneratorDevice::GetBatteryStatus -------------------------
    # The charger operates at one of three battery charging voltage
    # levels depending on ambient temperature.
    #  - 13.5VDC at High Temperature
    #  - 14.1VDC at Normal Temperature
    #  - 14.6VDC at Low Temperature
    # The battery charger is powered from a 120 VAC Load connection
    # through a fuse (F3) in the transfer switch. This 120 VAC source
    # must be connected to the Generator in order to operate the
    # charger.
    # During a Utility failure, the charger will momentarily be turned
    # off until the Generator is connected to the Load. During normal
    # operation, the battery charger supplies all the power to the
    # controller; the Generator battery is not used to supply power.
    # The battery charger will begin its charge cycle when battery
    # voltage drops below approximately 12.6V. The charger provides
    # current directly to the battery dependant on temperature, and the
    # battery is charged at the appropriate voltage level for 18 hours.
    # At the end of the 18 hour charge period battery charge current
    # is measured when the Generator is off. If battery charge current
    # at the end of the 18 hour charge time is greater than a pre-set
    # level, or the battery open-circuit voltage is less than approximately
    # 12.5V, an Inspect Battery warning is raised. If the engine cranks
    # during the 18 hour charge period, then the 18 hour charge timer
    # is restarted.
    # At the end of the 18 hour charge period the charger does one of
    # two things. If the temperature is less than approximately 40F
    # the battery is continuously charged at a voltage of 14.1V (i.e. the
    # charge voltage is changed from 14.6V to 14.1V after 18 hours). If
    # the temperature is above approximately 40F then the charger will
    # stop charging the battery after 18 hours.
    # The battery has a similar role as that found in an automobile
    # application. It sits doing nothing until it either self-discharges below
    # 12.6V or an engine crank occurs (i.e. such as occurs during the
    # weekly exercise cycle). If either condition occurs the battery charge
    # will begin its 18 hour charge cycle.
    def GetBatteryStatus(self):

        if not self.EvolutionController:
            return "Not Available"     # Nexus
        else:                           # Evolution
            if self.LiquidCooled:
                Register = "0053"
            else:
                return "Not Available"

        # get Battery Charging Voltage
        Value = self.GetRegisterValueFromList(Register)
        if len(Value) != 4:
            return ""

        Outputs = int(Value,16)

        if self.BitIsEqual(Outputs, 0x10, 0x10):
            return "Charging"
        else:
            return "Not Charging"

    #------------ GeneratorDevice::GetStatusForGUI ------------------------------------
    def GetStatusForGUI(self):

        Status = {}

        Status["basestatus"] = self.GetBaseStatus()
        Status["kwOutput"] = self.GetPowerOutput()
        Status["Exercise"] = self.GetParsedExerciseTime()
        Status["UnsentFeedback"] = str(os.path.isfile(self.FeedbackLogFile))

        return Status

    #------------ GeneratorDevice::GetBaseStatus ------------------------------------
    def GetBaseStatus(self):

        if self.SystemInAlarm():
            return "ALARM"

        if self.ServiceIsDue():
            return "SERVICEDUE"

        EngineValue = self.GetEngineState()
        SwitchValue = self.GetSwitchState()
        if "exercising" in EngineValue.lower():
            return "EXERCISING"
        elif "running" in EngineValue.lower():
            if "auto" in SwitchValue.lower():
                return "RUNNING"
            else:
                return "RUNNING-MANUAL"
        else:
            if "off" in SwitchValue.lower():
                return "OFF"
            elif "manual" in SwitchValue.lower():
                return "MANUAL"
            else:
                return "READY"

    #------------ GeneratorDevice::ServiceIsDue ------------------------------------
    def ServiceIsDue(self):

        # get Hours until next service
        Value = self.GetRegisterValueFromList("0001")
        if len(Value) != 8:
            return False

        HexValue = int(Value,16)

        # service due alarm?
        if self.BitIsEqual(HexValue,   0xFFF0FFFF, 0x0000001F):
            return True

        # get Hours until next service
        if self.EvolutionController:
            ServiceList = ["A","B"]

            for Service in ServiceList:
                Value = self.GetServiceDue(Service, NoUnits = True)
                if not len(Value):
                    continue

                if (int(Value) <= 1):
                    return True

        if not self.EvolutionController:

            ServiceList = ["OIL","AIR","SPARK","BATTERY","OTHER"]

            for Service in ServiceList:
                Value = self.GetServiceDue(Service, NoUnits = True)
                if not len(Value):
                    continue

                if (int(Value) <= 1):
                    return True

        return False

    #------------ GeneratorDevice::GetServiceDue ------------------------------------
    def GetServiceDue(self, serviceType = "A", NoUnits = False):

        ServiceTypeLookup_Evo = {
                                "A" : "001a",
                                "B" : "001e"
                                }
        ServiceTypeLookup_Nexus_AC = {
                                "SPARK" : "001a",
                                "OIL" : "001e",
                                "AIR" : "001c",
                                "BATTERY" : "001f",
                                "OTHER" : "0021"        # Do not know the corrposonding Due Date Register for this one
                                }
        ServiceTypeLookup_Nexus_LC = {
                                "OIL" : "001a",
                                "SPARK" : "001e",
                                "AIR" : "001c"
                                }
        if self.EvolutionController:
            LookUp = ServiceTypeLookup_Evo
        elif not self.LiquidCooled:
            LookUp = ServiceTypeLookup_Nexus_AC
        else:
            LookUp = ServiceTypeLookup_Nexus_LC

        Register = LookUp.get(serviceType.upper(), "")

        if not len(Register):
            return ""

        # get Hours until next service
        Value = self.GetRegisterValueFromList(Register)
        if len(Value) != 4:
            return ""

        if NoUnits:
            ServiceValue = "%d" % int(Value,16)
        else:
            ServiceValue = "%d hrs" % int(Value,16)

        return ServiceValue

    #------------ GeneratorDevice::GetServiceDueDate ------------------------------------
    def GetServiceDueDate(self, serviceType = "A"):

        # Evolution Air Cooled Maintenance Message Intervals
        #Inspect Battery"  1 Year
        #Schedule A       200 Hours or 2 years
        #Schedule B       400 Hours
        # Evolution Liquid Cooled Maintenance Message Intervals
        #Inspect Battery"  1000 Hours
        #Schedule A       125 Hours or 1 years
        #Schedule B       250 Hours or 2 years
        #Schedule C       1000 Hours
        ServiceTypeLookup_Evo = {
                                "A" : "001b",
                                "B" : "001f"
                                }

        # Nexus Air Cooled Maintenance Message Intervals
        # Inspect Battery     1 Year
        #Change Oil & Filter  200 Hours or 2 years
        #Inspect Air Filter   200 Hours or 2 years
        #Change Air Filter    200 Hours or 2 years
        #Inspect Spark Plugs  200 Hours or 2 years
        #Change spark Plugs   400 Hours or 10 years
        ServiceTypeLookup_Nexus_AC = {
                                "SPARK" : "001b",
                                "OIL" : "0020",
                                "BATTERY" : "001d",
                                "AIR": "0022"
                                }
        # Nexus Liquid Cooled Maintenance Message Intervals
        #Change oil & filter alert                  3mo/30hrs break-in 1yr/100hrs
        #inspect/clean air inlet & exhaust alert    3mo/30hrs break-in 6mo/50hrs
        #Change / inspect air filter alert          1yr/100hr
        #inspect spark plugs alert                  1yr/100hrs
        #Change / inspect spark plugs alert         2yr/250hr
        #inspect accessory drive alert              3mo/30hrs break-in 1yr/100hrs
        #Coolant change & flush                     1yr/100hrs
        #inspect battery alert                      1yr/100hrs
        ServiceTypeLookup_Nexus_LC = {
                                "OIL" : "001b",
                                "SPARK" : "001f",
                                "AIR" : "001d",
                                }
        if self.EvolutionController:
            LookUp = ServiceTypeLookup_Evo
        elif not self.LiquidCooled:
            LookUp = ServiceTypeLookup_Nexus_AC
        else:
            LookUp = ServiceTypeLookup_Nexus_LC

        Register = LookUp.get(serviceType.upper(), "")

        if not len(Register):
            return ""

        # get Hours until next service
        Value = self.GetRegisterValueFromList(Register)
        if len(Value) != 4:
            return ""

        try:
            time = int(Value,16) * 86400
            time += 86400
            Date = datetime.datetime.fromtimestamp(time)
            return Date.strftime('%m/%d/%Y ')
        except Exception as e1:
            self.LogError("Error in GetServiceDueDate: " + str(e1))
            return ""

    #----------  GeneratorDevice:GetHardwareVersion  ---------------------------------
    def GetHardwareVersion(self):

        Value = self.GetRegisterValueFromList("002a")
        if len(Value) != 4:
            return ""
        RegVal = int(Value, 16)

        IntTemp = RegVal >> 8           # high byte is firmware version
        FloatTemp = IntTemp / 100.0
        return "V%2.2f" % FloatTemp     #

    #----------  GeneratorDevice:GetFirmwareVersion  ---------------------------------
    def GetFirmwareVersion(self):
        Value = self.GetRegisterValueFromList("002a")
        if len(Value) != 4:
            return ""
        RegVal = int(Value, 16)

        IntTemp = RegVal & 0xff         # low byte is firmware version
        FloatTemp = IntTemp / 100.0
        return "V%2.2f" % FloatTemp     #

    #------------ GeneratorDevice::GetRunTimes ----------------------------------------
    def GetRunTimes(self):

        if not self.EvolutionController or not self.LiquidCooled:
            # get total hours running
            Value = self.GetRegisterValueFromList("000c")
            if len(Value) != 4:
                return ""

            TotalRunTimeLow = int(Value,16)

            # get total hours running
            Value = self.GetRegisterValueFromList("000b")
            if len(Value) != 4:
                return ""
            TotalRunTimeHigh = int(Value,16)

            TotalRunTime = (TotalRunTimeHigh << 16)| TotalRunTimeLow
            RunTimes = "%d " % (TotalRunTime)
        else:
            # total engine run time in minutes
            Value = self.GetRegisterValueFromList("005f")
            if len(Value) != 4:
                return ""

            TotalRunTimeLow = int(Value,16)

            Value = self.GetRegisterValueFromList("005e")
            if len(Value) != 4:
                return ""

            TotalRunTimeHigh = int(Value,16)

            TotalRunTime = (TotalRunTimeHigh << 16)| TotalRunTimeLow
            #hours, min = divmod(TotalRunTime, 60)
            #RunTimes = "Total Engine Run Time: %d:%d " % (hours, min)
            TotalRunTime = TotalRunTime / 60.0
            RunTimes = "%.2f " % (TotalRunTime)

        return RunTimes

   #-------------GeneratorDevice::GetSystemHealth--------------------------------
    #   returns the health of the monitor program
    def GetSystemHealth(self):

        outstr = ""
        if not self.InitComplete:
            outstr += "System Initializing. "
        if not self.AreThreadsAlive():
            outstr += " Threads are dead. "
        if  not self.CommunicationsActive:
            outstr += " Not receiving data. "

        if len(outstr) == 0:
            outstr = "OK"
        return outstr

    #----------  GeneratorDevice::DebugThread-------------------------------------
    def DebugThread(self):

        if not self.EnableDebug:
            return
        msgbody = "\n"
        while True:
            if len(self.RegistersUnderTestData):
                msgbody = self.RegistersUnderTestData
                self.RegistersUnderTestData = ""
            else:
                msgbody += "Nothing Changed"
            msgbody += "\n\n"
            count = 0
            for Register, Value in self.RegistersUnderTest.items():
                msgbody += self.printToScreen("%s:%s" % (Register, Value), True)

            self.mail.sendEmail("Register Under Test", msgbody, msgtype = "info")
            msgbody = ""

            for x in range(0, 60):
                for y in range(0, 10):
                    time.sleep(1)
                    if self.IsStopSignaled("DebugThread"):
                        return
    #----------  GeneratorDevice::removeAlpha--------------------------
    # used to remove alpha characters from string so the string contains a
    # float value (leaves all special characters)
    def removeAlpha(self, inputStr):
        answer = ""
        for char in inputStr:
            if not char.isalpha():
                answer += char
        return answer

    #----------  GeneratorDevice::TimeSyncThread-------------------------------------
    def TimeSyncThread(self):

        self.bDST = self.is_dst()   # set initial DST state

        while True:
            time.sleep(1)
            if self.InitComplete:
                break
            if self.IsStopSignaled("TimeSyncThread"):
                return

        # if we are not always syncing, then set the time once
        if not self.bSyncTime:
            SetTimeThread = threading.Thread(target=self.SetGeneratorTimeDate, name = "SetTimeThread")
            SetTimeThread.daemon = True
            SetTimeThread.start()               # start settime thread

        while True:

            if self.bSyncDST:
                if self.bDST != self.is_dst():  # has DST changed?
                    self.bDST = self.is_dst()   # update Flag
                    # time changed so some serial stats may be off
                    self.ModBus.Slave.ResetSerialStats()
                    # set new time
                    SetTimeThread = threading.Thread(target=self.SetGeneratorTimeDate, name = "SetTimeThread")
                    SetTimeThread.daemon = True
                    SetTimeThread.start()               # start settime thread
                    self.mail.sendEmail("Generator Time Update at " + self.SiteName, "Time updated due to daylight savings time change", msgtype = "info")

            if self.bSyncTime:
                # update gen time
                SetTimeThread = threading.Thread(target=self.SetGeneratorTimeDate, name = "SetTimeThread")
                SetTimeThread.daemon = True
                SetTimeThread.start()               # start settime thread

            for x in range(0, 60):
                for y in range(0, 60):
                    time.sleep(1)
                    if self.IsStopSignaled("TimeSyncThread"):
                        return

    #----------  GeneratorDevice::is_dst-------------------------------------
    def is_dst(self):
        #Determine whether or not Daylight Savings Time (DST) is currently in effect
        t = time.localtime()
        isdst = t.tm_isdst
        return (isdst != 0)

    #----------  GeneratorDevice::ComWatchDog-------------------------------------
    #----------  monitors receive data status to make sure we are still communicating
    def ComWatchDog(self):

        self.CommunicationsActive = False
        LastRxPacketCount = self.ModBus.Slave.RxPacketCount

        while True:

            if LastRxPacketCount == self.ModBus.Slave.RxPacketCount:
                self.CommunicationsActive = False
            else:
                self.CommunicationsActive = True
                LastRxPacketCount = self.ModBus.Slave.RxPacketCount
            time.sleep(2)

            if self.IsStopSignaled("ComWatchDog"):
                break

    #---------- GeneratorDevice:: AreThreadsAlive----------------------------------
    # ret true if all threads are alive
    def AreThreadsAlive(self):

        for Name, MyThreadObj in self.Threads.items():
            if not MyThreadObj.IsAlive():
                return False

        return True

    #---------- GeneratorDevice::GetDeadThreadName----------------------------------
    def GetDeadThreadName(self):

        RetStr = ""

        for Name, MyThreadObj in self.Threads.items():
            if not MyThreadObj.IsAlive():
                RetStr += MyThreadObj.Name() + " "

        if RetStr == "":
            RetStr = "None"

        return RetStr

    #----------  GeneratorDevice::SocketWorkThread-------------------------------------
    #  This thread spawns for each connection established by a client
    #  in InterfaceServerThread
    def SocketWorkThread(self, conn):

        try:

            conn.settimeout(2)   # only blok on recv for a small amount of time

            statusstr = ""
            if self.SystemInAlarm():
                statusstr += "CRITICAL: System in alarm! "
            HealthStr = self.GetSystemHealth()
            if HealthStr != "OK":
                statusstr += "WARNING: " + HealthStr
            if statusstr == "":
                statusstr = "OK "

            outstr = statusstr + ": "+ self.GetSwitchState() + ", " + self.GetEngineState()
            conn.sendall(outstr.encode())

            while True:
                try:
                    data = conn.recv(1024)

                    outstr = self.ProcessCommand(data, True)
                    conn.sendall(outstr.encode())
                except socket.timeout:
                    continue
                except socket.error as msg:
                    self.ConnectionList.remove(conn)
                    conn.close()
                    break

        except socket.error as msg:
            self.ConnectionList.remove(conn)
            conn.close()

        # end SocketWorkThread

    #----------  interface for heartbeat server thread -------------
    def InterfaceServerThread(self):

        #create an INET, STREAMing socket
        self.ServerSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # set some socket options so we can resuse the port
        self.ServerSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        #bind the socket to a host, and a port
        self.ServerSocket.bind(('', self.ServerSocketPort))
        #become a server socket
        self.ServerSocket.listen(5)

        #wait to accept a connection - blocking call
        while True:
            try:
                conn, addr = self.ServerSocket.accept()
                #self.printToScreen( 'Connected with ' + addr[0] + ':' + str(addr[1]))
                conn.settimeout(0.5)
                self.ConnectionList.append(conn)
                SocketThread = threading.Thread(target=self.SocketWorkThread, args = (conn,), name = "SocketWorkThread")
                SocketThread.daemon = True
                SocketThread.start()       # start server thread
            except Exception as e1:
                self.LogError("Excpetion in InterfaceServerThread" + str(e1))
                time.sleep(0.5)
                continue

        self.ServerSocket.close()
        #
    #---------------------GeneratorDevice::FatalError------------------------
    def LogError(self, Message):
        self.log.error(Message)
    #---------------------GeneratorDevice::FatalError------------------------
    def FatalError(self, Message):

        self.log.error(Message)
        raise Exception(Message)

    #---------------------GeneratorDevice::Close------------------------
    def Close(self):

        if self.MailInit:
            self.mail.sendEmail("Generator Monitor Stopping at " + self.SiteName, "Generator Monitor Stopping at " + self.SiteName, msgtype = "info" )

        for item in self.ConnectionList:
            try:
                item.close()
            except:
                continue
            self.ConnectionList.remove(item)

        if(self.ServerSocket):
            self.ServerSocket.shutdown(socket.SHUT_RDWR)
            self.ServerSocket.close()

        if self.ModBus.DeviceInit:
            self.ModBus.Slave.Close()

    #------------ GeneratorDevice::BitIsEqual -----------------------------------------
    def BitIsEqual(self, value, mask, bits):

        newval = value & mask
        if (newval == bits):
            return True
        else:
            return False

    #------------ GeneratorDevice::printToScreen --------------------------------------------
    def printToScreen(self, msgstr, outstr = False, nonewline = False, spacer = False):

        if spacer:
            MessageStr = "    {0}"
        else:
            MessageStr = "{0}"

        if not nonewline:
            MessageStr += "\n"

        if outstr == False:
            if self.bDisplayOutput:
                print (MessageStr.format(msgstr), end='')
            return ""
        else:
            newtpl = MessageStr.format(msgstr),
            return newtpl[0]

        # end printToScreen

#----------  Signal Handler ------------------------------------------
def signal_handler(signal, frame):


    sys.exit(0)

    # end signal_handler

#----------  print hex values  ---------------------------------------------
def printHexValues( buffer, separator1, separator2):

    # print in hex
    if(len(buffer) == 0):   # don't print if there is no data to print
        return

    new_str =  separator1
    for i in buffer:
        new_str += "%02x " % i

    new_str += separator2
    self.printToScreen (new_str)

#------------------- Command-line interface for monitor -----------------#
if __name__=='__main__': # usage SerialTest.py [baud_rate]


    # Set the signal handler
    signal.signal(signal.SIGINT, signal_handler)


    #Starting serial connection
    MyGen = GeneratorDevice()

    while True:
        time.sleep(1)

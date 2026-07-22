from sqlalchemy import Column, String, Float, DateTime, BigInteger, SmallInteger
from app.database.base import Base


class InverterDailyGen(Base):
    __tablename__ = "INVERTER_DAILY_GEN"
    __table_args__ = {"extend_existing": True}
    TimeCol         = Column(DateTime, primary_key=True)
    MSecCol         = Column(SmallInteger)
    LocalCol        = Column(DateTime)
    INVERTER_01_GEN = Column(BigInteger)
    INVERTER_02_GEN = Column(BigInteger)
    INVERTER_03_GEN = Column(BigInteger)
    INVERTER_04_GEN = Column(BigInteger)
    INVERTER_05_GEN = Column(BigInteger)
    INVERTER_06_GEN = Column(BigInteger)
    INVERTER_07_GEN = Column(BigInteger)
    INVERTER_08_GEN = Column(BigInteger)
    INVERTER_09_GEN = Column(BigInteger)
    INVERTER_10_GEN = Column(BigInteger)
    INVERTER_11_GEN = Column(BigInteger)
    INVERTER_12_GEN = Column(BigInteger)
    INVERTER_13_GEN = Column(BigInteger)
    INVERTER_14_GEN = Column(BigInteger)
    INVERTER_15_GEN = Column(BigInteger)
    INVERTER_16_GEN = Column(BigInteger)
    INVERTER_17_GEN = Column(BigInteger)
    INVERTER_18_GEN = Column(BigInteger)
    INVERTER_19_GEN = Column(BigInteger)
    INVERTER_20_GEN = Column(BigInteger)
    INVERTER_21_GEN = Column(BigInteger)
    INVERTER_22_GEN = Column(BigInteger)
    INVERTER_23_GEN = Column(BigInteger)
    INVERTER_24_GEN = Column(BigInteger)


class InverterMonthlyGen(Base):
    __tablename__ = "INVERTER_MONTHLY_GEN"
    __table_args__ = {"extend_existing": True}
    TimeCol         = Column(DateTime, primary_key=True)
    MSecCol         = Column(SmallInteger)
    LocalCol        = Column(DateTime)
    INVERTER_01_GEN = Column(BigInteger)
    INVERTER_02_GEN = Column(BigInteger)
    INVERTER_03_GEN = Column(BigInteger)
    INVERTER_04_GEN = Column(BigInteger)
    INVERTER_05_GEN = Column(BigInteger)
    INVERTER_06_GEN = Column(BigInteger)
    INVERTER_07_GEN = Column(BigInteger)
    INVERTER_08_GEN = Column(BigInteger)
    INVERTER_09_GEN = Column(BigInteger)
    INVERTER_10_GEN = Column(BigInteger)
    INVERTER_11_GEN = Column(BigInteger)
    INVERTER_12_GEN = Column(BigInteger)
    INVERTER_13_GEN = Column(BigInteger)
    INVERTER_14_GEN = Column(BigInteger)
    INVERTER_15_GEN = Column(BigInteger)
    INVERTER_16_GEN = Column(BigInteger)
    INVERTER_17_GEN = Column(BigInteger)
    INVERTER_18_GEN = Column(BigInteger)
    INVERTER_19_GEN = Column(BigInteger)
    INVERTER_20_GEN = Column(BigInteger)
    INVERTER_21_GEN = Column(BigInteger)
    INVERTER_22_GEN = Column(BigInteger)
    INVERTER_23_GEN = Column(BigInteger)
    INVERTER_24_GEN = Column(BigInteger)


class PowerGraph(Base):
    __tablename__ = "POWER_GRAPH"
    __table_args__ = {"extend_existing": True}
    TimeCol                  = Column(DateTime, primary_key=True)
    MSecCol                  = Column(SmallInteger)
    LocalCol                 = Column(DateTime)
    INVERTER_01_ACTIVE_POWER = Column(Float)
    INVERTER_02_ACTIVE_POWER = Column(Float)
    INVERTER_03_ACTIVE_POWER = Column(Float)
    INVERTER_04_ACTIVE_POWER = Column(Float)
    INVERTER_05_ACTIVE_POWER = Column(Float)
    INVERTER_06_ACTIVE_POWER = Column(Float)
    INVERTER_07_ACTIVE_POWER = Column(Float)
    INVERTER_08_ACTIVE_POWER = Column(Float)
    INVERTER_09_ACTIVE_POWER = Column(Float)
    INVERTER_10_ACTIVE_POWER = Column(Float)
    INVERTER_11_ACTIVE_POWER = Column(Float)
    INVERTER_12_ACTIVE_POWER = Column(Float)
    INVERTER_13_ACTIVE_POWER = Column(Float)
    INVERTER_14_ACTIVE_POWER = Column(Float)
    INVERTER_15_ACTIVE_POWER = Column(Float)
    INVERTER_16_ACTIVE_POWER = Column(Float)
    INVERTER_17_ACTIVE_POWER = Column(Float)
    INVERTER_18_ACTIVE_POWER = Column(Float)
    INVERTER_19_ACTIVE_POWER = Column(Float)
    INVERTER_20_ACTIVE_POWER = Column(Float)
    INVERTER_21_ACTIVE_POWER = Column(Float)
    INVERTER_22_ACTIVE_POWER = Column(Float)
    INVERTER_23_ACTIVE_POWER = Column(Float)
    INVERTER_24_ACTIVE_POWER = Column(Float)


class PowerVsIrradiance(Base):
    __tablename__ = "POWER_VS_IRRADIANCE"
    __table_args__ = {"extend_existing": True}
    TimeCol                        = Column(DateTime, primary_key=True)
    MSecCol                        = Column(SmallInteger)
    LocalCol                       = Column(DateTime)
    POWER_GENERATION               = Column(Float)
    AVG_ALBEDO_UP_CUMM_IRRADIATION = Column(Float)
    AVG_GHI_IRRADIATION            = Column(Float)


class PPCData(Base):
    __tablename__ = "PPC"
    __table_args__ = {"extend_existing": True}
    TimeCol                       = Column(DateTime, primary_key=True)
    MSecCol                       = Column(SmallInteger)
    LocalCol                      = Column(DateTime)
    ACTIVE_POWER_SET_POINT        = Column(Float)
    GRID_ACTIVE_POWER_MEASURED    = Column(Float)
    GRID_FREQUENCY_MEASURED       = Column(Float)
    GRID_PF_MEASURED              = Column(Float)
    GRID_REACTIVE_POWER_MEASURED  = Column(Float)
    GRID_VOLTAGE_L_L_MEASURED     = Column(Float)
    INVERTER_RUNNING              = Column(BigInteger)
    INVERTER_TOTAL_ACTIVE_POWER   = Column(Float)
    INVERTER_TOTAL_REACTIVE_POWER = Column(Float)
    PLANT_DAILY_PRODUCTION        = Column(Float)
    PLANT_MONTHLY_PRODUCTION      = Column(Float)
    PLANT_YEARLY_PRODUCTION       = Column(Float)
    PLANT_LIFETIME_PRODUCTION     = Column(Float)
    DAILY_OPERATING_TIME          = Column(BigInteger)
    MONTHLY_OPERATING_TIME        = Column(BigInteger)


class WMSData(Base):
    __tablename__ = "WMS"
    __table_args__ = {"extend_existing": True}
    TimeCol                        = Column(DateTime, primary_key=True)
    MSecCol                        = Column(SmallInteger)
    LocalCol                       = Column(DateTime)
    AVG_AIR_PRESSURE               = Column(Float)
    AVG_AIR_TEMP                   = Column(Float)
    AVG_ALBEDO_UP_CUMM_IRRADIATION = Column(Float)
    AVG_ALBEDO_UP_IRRADIATION      = Column(Float)
    AVG_GHI_CUMM_IRRADIATION       = Column(Float)
    AVG_GHI_IRRADIATION            = Column(Float)
    AVG_GTI_CUMM_IRRADIATION       = Column(Float)
    AVG_GTI_IRRADIATION            = Column(Float)
    AVG_RELATIVE_HUMIDITY          = Column(Float)
    AVG_WIND_DIRECTION             = Column(Float)
    AVG_WIND_SPEED                 = Column(Float)
    AVG_WMS1_MODULE_TEMP           = Column(Float)
    AVG_WMS2_MODULE_TEMP           = Column(Float)
    AVG_WMS3_MODULE_TEMP           = Column(Float)
    AVG_WMS4_MODULE_TEMP           = Column(Float)
    AVG_IR_BACKPLANE_TEMP          = Column(Float)
    AVG_IR_SOILING_RATIO1          = Column(Float)
    AVG_IR_SOILING_RATIO2          = Column(Float)
    TOTAL_IRRADIANCE               = Column(Float)
    ALL_WMS_AVG_MODULE_TEMP        = Column(Float)
    AVG_PRECIPITATION_INTENSITY    = Column(Float)


class AlarmsData(Base):
    __tablename__ = "Alarms"
    __table_args__ = {"extend_existing": True}
    TimeCol   = Column(DateTime, primary_key=True)
    MSecCol   = Column(SmallInteger)
    LocalCol  = Column(DateTime)
    UserCol   = Column(String(255))
    EventCol  = Column(String(24))
    EvNumCol  = Column(SmallInteger)
    EvDescCol = Column(String(255))
    DescCol   = Column(String(255))
    CommCol   = Column(String(255))
    DurCol    = Column(BigInteger)
    UniID     = Column(BigInteger)
    TraID     = Column(BigInteger)


class TemperatureReport(Base):
    __tablename__ = "TEMPERATURE_REPORT"
    __table_args__ = {"extend_existing": True}
    TimeCol                = Column(DateTime, primary_key=True)
    MSecCol                = Column(SmallInteger)
    LocalCol               = Column(DateTime)
    AC_BREAKER_TEMP_18     = Column(Float)
    AC_BREAKER_TEMP_23     = Column(Float)
    AMBIENT_BOTTOM_TEMP_18 = Column(Float)
    AMBIENT_BOTTOM_TEMP_23 = Column(Float)
    BECHKOFF_TEMP_18       = Column(Float)
    BECHKOFF_TEMP_23       = Column(Float)
    CTRL_BOTTOM_18         = Column(Float)
    CTRL_BOTTOM_23         = Column(Float)
    DC_INCOMER_18          = Column(Float)
    DC_INCOMER_23          = Column(Float)
    OUT_HEAT_SINK_PSU1_18  = Column(Float)
    OUT_HEAT_SINK_PSU1_23  = Column(Float)
    OUT_HEAT_SINK_PSU2_18  = Column(Float)
    OUT_HEAT_SINK_PSU2_23  = Column(Float)
    PEC_18                 = Column(Float)
    PEC_23                 = Column(Float)
"""Wisp: WiFi Spatial Presence node core, as an ESPHome external component."""

import esphome.codegen as cg
from esphome.components import sensor
from esphome.components.esp32 import add_idf_sdkconfig_option
import esphome.config_validation as cv
from esphome.const import (
    CONF_ID,
    ENTITY_CATEGORY_DIAGNOSTIC,
    STATE_CLASS_MEASUREMENT,
    STATE_CLASS_TOTAL_INCREASING,
)

CODEOWNERS = ["@albert-canfield"]
DEPENDENCIES = ["esp32", "wifi"]
AUTO_LOAD = ["sensor"]

CONF_AP_PING_INTERVAL = "ap_ping_interval"
CONF_RAW_STREAM_PORT = "raw_stream_port"
CONF_AP_CSI_RATE = "ap_csi_rate"
CONF_CSI_DROPPED = "csi_dropped"

wisp_ns = cg.esphome_ns.namespace("wisp")
WispComponent = wisp_ns.class_("WispComponent", cg.Component)

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(WispComponent),
        cv.Optional(CONF_AP_PING_INTERVAL, default="50ms"): cv.All(
            cv.positive_time_period_milliseconds,
            cv.Range(min=cv.TimePeriod(milliseconds=10)),
        ),
        cv.Optional(CONF_RAW_STREAM_PORT, default=47010): cv.port,
        cv.Optional(CONF_AP_CSI_RATE): sensor.sensor_schema(
            unit_of_measurement="Hz",
            icon="mdi:access-point",
            accuracy_decimals=1,
            state_class=STATE_CLASS_MEASUREMENT,
            entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
        ),
        cv.Optional(CONF_CSI_DROPPED): sensor.sensor_schema(
            icon="mdi:tray-alert",
            accuracy_decimals=0,
            state_class=STATE_CLASS_TOTAL_INCREASING,
            entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
        ),
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    add_idf_sdkconfig_option("CONFIG_ESP_WIFI_CSI_ENABLED", True)

    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(var.set_ap_ping_interval(config[CONF_AP_PING_INTERVAL]))
    cg.add(var.set_raw_stream_port(config[CONF_RAW_STREAM_PORT]))
    if CONF_AP_CSI_RATE in config:
        sens = await sensor.new_sensor(config[CONF_AP_CSI_RATE])
        cg.add(var.set_ap_csi_rate_sensor(sens))
    if CONF_CSI_DROPPED in config:
        sens = await sensor.new_sensor(config[CONF_CSI_DROPPED])
        cg.add(var.set_csi_dropped_sensor(sens))

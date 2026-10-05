"""Wisp: WiFi Spatial Presence node core, as an ESPHome external component."""

import esphome.codegen as cg
from esphome.components import binary_sensor, sensor
from esphome.components.esp32 import add_idf_sdkconfig_option
import esphome.config_validation as cv
from esphome.const import (
    CONF_ID,
    DEVICE_CLASS_MOTION,
    ENTITY_CATEGORY_DIAGNOSTIC,
    STATE_CLASS_MEASUREMENT,
    STATE_CLASS_TOTAL_INCREASING,
)

CODEOWNERS = ["@albert-canfield"]
DEPENDENCIES = ["esp32", "wifi"]
AUTO_LOAD = ["binary_sensor", "sensor"]

CONF_AP_PING_INTERVAL = "ap_ping_interval"
CONF_RAW_STREAM_PORT = "raw_stream_port"
CONF_AP_CSI_RATE = "ap_csi_rate"
CONF_CSI_DROPPED = "csi_dropped"
CONF_AP_MOTION_SCORE = "ap_motion_score"
CONF_AP_MOTION = "ap_motion"
CONF_MOTION_THRESHOLD = "motion_threshold"
CONF_REPORT_INTERVAL = "report_interval"
CONF_GRID_NODES = "grid_nodes"
CONF_GRID_CHANNEL = "grid_channel"
CONF_AP_MIN_RSSI = "ap_min_rssi"
CONF_HIVE_IN_SYNC = "hive_in_sync"
CONF_CHANNEL = "channel"

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
        cv.Optional(CONF_REPORT_INTERVAL, default="200ms"): cv.All(
            cv.positive_time_period_milliseconds,
            cv.Range(min=cv.TimePeriod(milliseconds=50)),
        ),
        cv.Optional(CONF_GRID_CHANNEL, default=0): cv.int_range(min=0, max=14),
        cv.Optional(CONF_AP_MIN_RSSI, default=-80): cv.int_range(min=-100, max=-30),
        cv.Optional(CONF_HIVE_IN_SYNC): binary_sensor.binary_sensor_schema(
            icon="mdi:hexagon-multiple",
            entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
        ),
        cv.Optional(CONF_CHANNEL): sensor.sensor_schema(
            icon="mdi:wifi-cog",
            accuracy_decimals=0,
            entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
        ),
        cv.Optional(CONF_GRID_NODES): sensor.sensor_schema(
            icon="mdi:hexagon-multiple-outline",
            accuracy_decimals=0,
            state_class=STATE_CLASS_MEASUREMENT,
            entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
        ),
        cv.Optional(CONF_MOTION_THRESHOLD, default=2.0): cv.float_range(min=1.0, min_included=False),
        cv.Optional(CONF_AP_MOTION_SCORE): sensor.sensor_schema(
            icon="mdi:motion-sensor",
            accuracy_decimals=2,
            state_class=STATE_CLASS_MEASUREMENT,
        ),
        cv.Optional(CONF_AP_MOTION): binary_sensor.binary_sensor_schema(
            device_class=DEVICE_CLASS_MOTION,
        ),
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
    cg.add(var.set_motion_threshold(config[CONF_MOTION_THRESHOLD]))
    cg.add(var.set_report_interval(config[CONF_REPORT_INTERVAL]))
    cg.add(var.set_grid_channel(config[CONF_GRID_CHANNEL]))
    cg.add(var.set_ap_min_rssi(config[CONF_AP_MIN_RSSI]))
    if CONF_HIVE_IN_SYNC in config:
        bs = await binary_sensor.new_binary_sensor(config[CONF_HIVE_IN_SYNC])
        cg.add(var.set_hive_sync_binary_sensor(bs))
    if CONF_CHANNEL in config:
        sens = await sensor.new_sensor(config[CONF_CHANNEL])
        cg.add(var.set_channel_sensor(sens))
    if CONF_GRID_NODES in config:
        sens = await sensor.new_sensor(config[CONF_GRID_NODES])
        cg.add(var.set_grid_nodes_sensor(sens))
    if CONF_AP_MOTION_SCORE in config:
        sens = await sensor.new_sensor(config[CONF_AP_MOTION_SCORE])
        cg.add(var.set_ap_motion_score_sensor(sens))
    if CONF_AP_MOTION in config:
        bs = await binary_sensor.new_binary_sensor(config[CONF_AP_MOTION])
        cg.add(var.set_ap_motion_binary_sensor(bs))
    if CONF_AP_CSI_RATE in config:
        sens = await sensor.new_sensor(config[CONF_AP_CSI_RATE])
        cg.add(var.set_ap_csi_rate_sensor(sens))
    if CONF_CSI_DROPPED in config:
        sens = await sensor.new_sensor(config[CONF_CSI_DROPPED])
        cg.add(var.set_csi_dropped_sensor(sens))

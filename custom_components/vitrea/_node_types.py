"""Node type -> category mapping (from Node.TypeCategory in the decompiled app).

The node_type byte is the same value in both V-Box protocols.
"""
from __future__ import annotations


# Node.NodeType → category string (from Node.TypeCategory in decompiled source)
_TOGGLE_TYPES    = {1,2,3,4,5,20,21,22,23,26,27,31,63}
_SATELLITE_TYPES = {11,12,13,14,15}
_BLIND_TYPES     = {6,7,8,9,10,16,17,18,19,24,25,28,29,90,127}
_DIMMER_TYPES    = {32,33,34,35,36,37,38,39,40,41,42,43,44,45,64,65,66,67,68,69,70,71,72,73,74,75,76,77,78,79,91,92}
_BOILER_TYPES    = {30}
_SENSOR_TYPES    = {50,51,52,53,54,55,56,57,60}
_REPEATER_TYPES  = {61}
_THERMO_TYPES    = {80}

def _node_category(type_id: int) -> str:
    if type_id in _TOGGLE_TYPES:    return "Toggle"
    if type_id in _SATELLITE_TYPES: return "Satellite"
    if type_id in _BLIND_TYPES:     return "Blind"
    if type_id in _DIMMER_TYPES:    return "Dimmer"
    if type_id in _BOILER_TYPES:    return "Boiler"
    if type_id in _SENSOR_TYPES:    return "Sensor"
    if type_id in _REPEATER_TYPES:  return "Repeater"
    if type_id in _THERMO_TYPES:    return "Thermo"
    if type_id == 49:               return "DualPole"
    if type_id == 62:               return "Fan"
    return f"type{type_id}"


node_category = _node_category

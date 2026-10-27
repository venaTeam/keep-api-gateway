import json
import logging
from typing import List

import celpy
import celpy.c7nlib
import celpy.celparser
import celpy.celtypes
import celpy.evaluation

from src.models.alert import AlertDto, AlertSeverity
from src.utils.cel_utils import preprocess_cel_expression

# Shahar: this is performance enhancment https://github.com/cloud-custodian/cel-python/issues/68


celpy.evaluation.Referent.__repr__ = lambda self: ""
celpy.evaluation.NameContainer.__repr__ = lambda self: ""
celpy.Activation.__repr__ = lambda self: ""
celpy.Activation.__str__ = lambda self: ""
celpy.celtypes.MapType.__repr__ = lambda self: ""
celpy.celtypes.DoubleType.__repr__ = lambda self: ""
celpy.celtypes.BytesType.__repr__ = lambda self: ""
celpy.celtypes.IntType.__repr__ = lambda self: ""
celpy.celtypes.UintType.__repr__ = lambda self: ""
celpy.celtypes.ListType.__repr__ = lambda self: ""
celpy.celtypes.StringType.__repr__ = lambda self: ""
celpy.celtypes.TimestampType.__repr__ = lambda self: ""
celpy.c7nlib.C7NContext.__repr__ = lambda self: ""
celpy.celparser.Tree.__repr__ = lambda self: ""


class RulesEngine:
    def __init__(self, tenant_id=None):
        self.tenant_id = tenant_id
        self.logger = logging.getLogger(__name__)
        self.env = celpy.Environment()

    @staticmethod
    def sanitize_cel_payload(payload):
        """
        Remove keys containing forbidden characters from payload and return warnings.
        Returns tuple of (sanitized_payload, warnings)
        """
        forbidden_starts = [
            "@",
            "-",
            "$",
            "#",
            " ",
            ":",
            ".",
            "/",
            "\\",
            "*",
            "&",
            "^",
            "%",
            "!",
        ]
        logger = logging.getLogger(__name__)

        def _sanitize_dict(d):
            result = {}
            for k, v in d.items():
                if k[0] in forbidden_starts:  # Only check first character
                    logger.warning(
                        f"Removed key '{k}' starting with forbidden character '{k[0]}'"
                    )
                    continue

                if isinstance(v, dict):
                    result[k] = _sanitize_dict(v)
                elif isinstance(v, list):
                    result[k] = [
                        _sanitize_dict(i) if isinstance(i, dict) else i for i in v
                    ]
                else:
                    result[k] = v
            return result

        sanitized = _sanitize_dict(payload)
        return sanitized

    def _coerce_eq_type_error(self, cel, prgm, activation, alert):
        """
        Helper for type coercion fallback for ==/!= between int and str in CEL.
        Fixes https://github.com/keephq/keep/issues/5107
        """
        import re

        m = re.match(r"([a-zA-Z0-9_\.]+)\s*([!=]=)\s*(.+)", cel)
        if not m:
            return False
        left, op, right = m.groups()
        left = left.strip()
        right = (
            right.strip().strip('"')
            if right.strip().startswith('"') and right.strip().endswith('"')
            else right.strip()
        )
        try:

            def get_nested(d, path):
                for part in path.split("."):
                    if isinstance(d, dict):
                        d = d.get(part)
                    else:
                        return None
                return d

            left_val = get_nested(activation, left)
            try:
                right_val = int(right)
            except Exception:
                try:
                    right_val = float(right)
                except Exception:
                    right_val = right
            # If one is str and the other is int/float, compare as str
            if (isinstance(left_val, (int, float)) and isinstance(right_val, str)) or (
                isinstance(left_val, str) and isinstance(right_val, (int, float))
            ):
                if op == "==":
                    return str(left_val) == str(right_val)
                else:
                    return str(left_val) != str(right_val)
            # Also handle both as str for robustness
            if op == "==":
                return str(left_val) == str(right_val)
            else:
                return str(left_val) != str(right_val)
        except Exception:
            pass
        return False

    @staticmethod
    def get_alerts_activation(alerts: list[AlertDto]):
        activations = []
        for alert in alerts:
            payload = alert.dict()
            # TODO: workaround since source is a list
            #       should be fixed in the future
            payload["source"] = ",".join(payload["source"])
            # payload severity could be the severity itself or the order of the severity, cast it to the order
            if isinstance(payload["severity"], str):
                payload["severity"] = AlertSeverity(payload["severity"].lower()).order

            # sanitize the payload
            payload = RulesEngine.sanitize_cel_payload(payload)
            activation = celpy.json_to_cel(json.loads(json.dumps(payload, default=str)))
            activations.append(activation)
        return activations

    def filter_alerts(
        self, alerts: list[AlertDto], cel: str, alerts_activation: list = None
    ):
        """This function filters alerts according to a CEL

        Args:
            alerts (list[AlertDto]): list of alerts
            cel (str): CEL expression

        Returns:
            list[AlertDto]: list of alerts that are related to the cel
        """
        logger = logging.getLogger(__name__)
        # if the cel is empty, return all the alerts
        if cel == "":
            return alerts
        # if the cel is empty, return all the alerts
        if not cel:
            logger.debug("No CEL expression provided")
            return alerts
        # preprocess the cel expression
        cel = preprocess_cel_expression(cel)
        ast = self.env.compile(cel)
        prgm = self.env.program(ast)
        filtered_alerts = []

        for i, alert in enumerate(alerts):
            if alerts_activation:
                activation = alerts_activation[i]
            else:
                activation = self.get_alerts_activation([alert])[0]
            try:
                r = prgm.evaluate(activation)
            except ValueError as e:
                if "Invalid name" in str(e):
                    logger.warning(
                        f"{str(e)} in the CEL expression {cel} for alert {alert.id}. This might mean there's a blank space in the field name",
                        extra={"alert_id": alert.id, "payload": alert.dict()},
                    )
                    continue
            except celpy.evaluation.CELEvalError as e:
                # this is ok, it means that the subrule is not relevant for this event
                if "no such member" in str(e):
                    continue
                # unknown
                elif "no such overload" in str(
                    e
                ) or "found no matching overload" in str(e):
                    # Try type coercion for == and !=
                    try:
                        coerced = self._coerce_eq_type_error(
                            cel, prgm, activation, alert
                        )
                        if coerced:
                            filtered_alerts.append(alert)
                            continue
                    except Exception:
                        pass
                    logger.debug(
                        f"Type mismtach between operator and operand in the CEL expression {cel} for alert {alert.id}"
                    )
                    continue
                logger.warning(
                    f"Failed to evaluate the CEL expression {cel} for alert {alert.id} - {e}"
                )
                continue
            except Exception:
                logger.exception(
                    f"Failed to evaluate the CEL expression {cel} for alert {alert.id}"
                )
                continue
            if r:
                filtered_alerts.append(alert)

        return filtered_alerts


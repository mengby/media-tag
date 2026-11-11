"""Independent offline ten-field evaluation; synthetic results never approve business use."""
from .models import Dataset,Export
from .runner import evaluate
from .calibration import propose_rules

__all__=['Dataset','Export','evaluate','propose_rules']

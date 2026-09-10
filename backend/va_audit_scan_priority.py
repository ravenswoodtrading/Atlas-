"""Temporarily pause the discovery queue and restore its exact previous state."""
import json
import sys
from app.services.scan_queue_service import ScanQueueService
from va_price_drop_audit import FOLDER

path = FOLDER/'scan_state.json'
if sys.argv[1] == 'pause':
    if not path.exists():
        path.write_text(json.dumps({'paused': ScanQueueService.is_paused()}))
    ScanQueueService.set_paused(True)
    print('Discovery queue paused:', ScanQueueService.is_paused())
elif sys.argv[1] == 'restore':
    state = json.loads(path.read_text())
    ScanQueueService.set_paused(state['paused'])
    print('Restored discovery queue paused:', ScanQueueService.is_paused())

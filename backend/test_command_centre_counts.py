"""Exercise actual route helpers without importing network client dependencies."""
import ast
from pathlib import Path
import unittest
import time
from unittest.mock import Mock


def functions(path, names, scope):
    tree = ast.parse(Path(path).read_text(encoding='utf-8-sig'))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=selected, type_ignores=[]), path, 'exec'), scope)


class CountTests(unittest.TestCase):
    def test_command_centre_uses_review_membership(self):
        scope = dict(time=time, QUEUE_PRIORITY_BUY_NOW='BUY_NOW', QUEUE_PRIORITY_VA_TO_REVIEW='VA_TO_REVIEW',
            QUEUE_PRIORITY_BORDERLINE='BORDERLINE', QUEUE_PRIORITY_NEEDS_ATTENTION='NEEDS_ATTENTION',
            MONITOR_ONLY_ACTIONS={'WATCH','HISTORICAL_RECURRING','BLOCKED'},
            VIEW_FILTERS=dict(buy_now='BUY_NOW', va_to_review='VA_TO_REVIEW', borderline='BORDERLINE',
                              needs_attention='NEEDS_ATTENTION', oa_investigate='OA_INVESTIGATE'),
            COMMAND_CENTRE_PREVIEW_SIZE=5)
        functions('app/routes/review_queue.py', {'_is_monitor_only','_matches_workflow_view','_requires_human_review'}, scope)
        functions('app/routes/dashboard.py', {'_command_centre'}, scope)
        items = [
            dict(views=['BORDERLINE'], sources=['scan'], action='WATCH'),
            dict(views=['NEEDS_ATTENTION'], sources=['scan'], action='INVESTIGATE'),
            dict(views=['BUY_NOW'], sources=['scan'], action='BUY', conflict=True),
            dict(views=['BORDERLINE'], sources=['lead'], action='WATCH'),
            dict(views=['OA_INVESTIGATE'], sources=['oa_investigate'], action='BLOCKED'),
        ]
        scope['ReviewQueueService'] = Mock()
        scope['ReviewQueueService'].list_queue_items.return_value = items
        result = scope['_command_centre']()
        self.assertEqual(result['counts']['BORDERLINE'], 3)
        self.assertEqual(result['unique_total'], 4)
        for view in ('buy_now', 'borderline', 'va_to_review', 'oa_investigate'):
            expected = sum(bool(scope['_matches_workflow_view'](i, view)) for i in items)
            self.assertEqual(result['counts'][scope['VIEW_FILTERS'][view]], expected)
        scope['ReviewQueueService'].list_queue_items.assert_called_once()

if __name__ == '__main__':
    unittest.main()

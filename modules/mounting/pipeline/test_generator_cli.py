import subprocess,sys,unittest
from pathlib import Path
class GeneratorCliTests(unittest.TestCase):
    def test_limit_accepts_1024_rejects_1025_before_reading_tree(self):
        script=Path(__file__).with_name('generate_semantic_boundaries.py')
        args=[sys.executable,str(script),'--tree','__missing_tree__','--out','__not_created__','--base','http://unused','--model','unused','--workers']
        r=subprocess.run(args+['1024'],capture_output=True,text=True)
        self.assertIn('FileNotFoundError',r.stderr)
        r=subprocess.run(args+['1025'],capture_output=True,text=True)
        self.assertIn('invalid workers',r.stderr)

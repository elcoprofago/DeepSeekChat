"""Pruebas de gpumon.find_command: orden de búsqueda y qué pasa si no hay nada."""
import os
import tempfile
import unittest

import gpumon


class T(unittest.TestCase):
    def test_orden(self):
        with tempfile.TemporaryDirectory() as d:
            app = os.path.join(d, "app"); os.mkdir(app)
            nada = lambda n: None
            self.assertIsNone(gpumon.find_command(app, nada))
            self.assertEqual(gpumon.find_command(app, lambda n: "U" if n == "uvx" else None), ["U", "gputaskman"])
            self.assertEqual(gpumon.find_command(app, lambda n: "G" if n == "gputaskman" else "U"), ["G"])
            exe = os.path.join(d, "gputaskman.exe"); open(exe, "w").close()
            self.assertEqual(gpumon.find_command(app, nada), [exe])
            exe2 = os.path.join(app, "gputaskman.exe"); open(exe2, "w").close()
            self.assertEqual(gpumon.find_command(app, nada), [exe2])
            b = os.path.join(d, "bin", "gputaskman"); os.makedirs(b)
            exe3 = os.path.join(b, "gputaskman.exe"); open(exe3, "w").close()
            self.assertEqual(gpumon.find_command(app, nada), [exe3])


if __name__ == "__main__":
    unittest.main()

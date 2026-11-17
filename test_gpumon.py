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
            self.assertIsNone(gpumon.find_command(app, "", nada))
            self.assertEqual(gpumon.find_command(app, "", lambda n: "G" if n == "gputaskman" else None), ["G"])
            exe = os.path.join(d, "gputaskman.exe"); open(exe, "w").close()
            self.assertEqual(gpumon.find_command(app, "", nada), [exe])
            exe2 = os.path.join(app, "gputaskman.exe"); open(exe2, "w").close()
            self.assertEqual(gpumon.find_command(app, "", nada), [exe2])
            b = os.path.join(d, "bin", "gputaskman"); os.makedirs(b)
            exe3 = os.path.join(b, "gputaskman.exe"); open(exe3, "w").close()
            self.assertEqual(gpumon.find_command(app, "", nada), [exe3])

    def test_carpeta_de_datos(self):
        with tempfile.TemporaryDirectory() as d:
            app = os.path.join(d, "app"); os.mkdir(app)
            data = os.path.join(d, "data")
            exe = gpumon.data_exe(data); os.makedirs(os.path.dirname(exe)); open(exe, "w").close()
            self.assertEqual(gpumon.find_command(app, data, lambda n: None), [exe])

    def test_extract_zip_real(self):
        z = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", "cache", gpumon.ZIP)
        if not os.path.isfile(z):
            self.skipTest("sin el zip en dist\\cache")
        gpumon.check_zip(z)
        with tempfile.TemporaryDirectory() as d:
            self.assertGreater(gpumon.extract(z, d), 100)
            self.assertTrue(os.path.isfile(os.path.join(d, "gputaskman.exe")))


if __name__ == "__main__":
    unittest.main()

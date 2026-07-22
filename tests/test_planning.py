import tempfile
import unittest
from pathlib import Path

from omar_ai_core.planning import PlanManager, format_plan_for_prompt, is_complex_request


class PlanningTests(unittest.TestCase):
    def test_complexity_detection_ignores_simple_commands(self):
        self.assertFalse(is_complex_request("Abre Google"))
        self.assertFalse(is_complex_request("¿Qué tiempo hace?"))
        self.assertTrue(
            is_complex_request(
                "Primero abre el navegador, busca la documentación y después crea un resumen y verifica los enlaces"
            )
        )

    def test_plan_requires_verified_steps_before_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = PlanManager(Path(temporary) / "active_plan.json")
            plan = manager.start("Preparar informe", ["Recopilar datos", "Verificar resultados"])
            self.assertEqual(plan["status"], "active")
            with self.assertRaises(ValueError):
                manager.finish("completed")
            manager.update(1, "completed", "Datos recopilados")
            manager.update(2, "completed", "Resultados comprobados")
            completed = manager.finish("completed", "Informe listo")
            self.assertEqual(completed["status"], "completed")

    def test_active_plan_survives_restart_and_is_formatted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "active_plan.json"
            PlanManager(path).start("Configurar equipo", ["Abrir ajustes", "Comprobar audio"])
            restored = PlanManager(path).load()
            prompt = format_plan_for_prompt(restored)
            self.assertIn("Configurar equipo", prompt)
            self.assertIn("[pending] Abrir ajustes", prompt)


if __name__ == "__main__":
    unittest.main()

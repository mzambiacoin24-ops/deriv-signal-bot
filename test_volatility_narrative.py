import unittest

from smc import SMCAnalyzer


class VolatilityNarrativeTests(unittest.TestCase):
    def _engine(self):
        engine = SMCAnalyzer("R_100", lookback=2, history=300)
        epoch = 0
        for i in range(40):
            open_price = 105.0 + (i % 2) * 0.2
            close_price = open_price + 0.1
            high = close_price + 0.3
            low = open_price - 0.3
            epoch += 60
            engine.add_candle({
                "epoch": epoch,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close_price,
            })
        return engine, epoch

    def _feed(self, engine, epoch, rows):
        result = None
        for open_price, high, low, close in rows:
            epoch += 60
            result = engine.add_candle({
                "epoch": epoch,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
            })
        return epoch, result

    def test_sweep_alone_does_not_signal(self):
        engine, epoch = self._engine()
        epoch, result = self._feed(engine, epoch, [
            (108, 111, 107.8, 110.5),
            (110.5, 112, 110, 111.5),
            (111.5, 112.5, 109, 109.2),
        ])
        self.assertIsNone(result)
        self.assertEqual(engine.pending_stage, "SWEEP")

    def test_complete_sequence_produces_setup(self):
        engine, epoch = self._engine()
        epoch, result = self._feed(engine, epoch, [
            (108, 111, 107.8, 110.5),
            (110.5, 112, 110, 111.5),
            (111.5, 112.5, 109, 109.2),
            (109.2, 109.4, 106, 106.2),
            (106.2, 106.5, 103.5, 103.8),
            (103.8, 104, 100.5, 101.0),
            (101.0, 105.5, 100.8, 104.5),
            (104.5, 104.7, 101.5, 102.0),
        ])
        self.assertIsNotNone(result)
        self.assertEqual(result["direction"], "down")
        self.assertEqual(result["sweep"], "high")
        self.assertIn("DISPLACEMENT", result["timing"])
        self.assertIn("MSS", result["timing"])
        self.assertIn("FVG", result["timing"])
        self.assertIn("RETRACEMENT", result["timing"])
        self.assertIn("CONFIRMATION", result["timing"])


if __name__ == "__main__":
    unittest.main()

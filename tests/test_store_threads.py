import threading
import unittest

from app.store import Store


class SharedConnection(unittest.TestCase):
    def test_concurrent_reads_and_writes_never_raise(self):
        st = Store()
        errors = []

        def writer(n):
            try:
                for i in range(300):
                    st.set_agent("a%d" % n, {"hard_cap": float(i)})
                    st.llm_bump("2026-11-03", calls=1)
            except Exception as e:                                       # noqa: BLE001
                errors.append(e)

        def reader():
            try:
                for _ in range(600):
                    st.due_cooldowns(1e12); st.agents(); st.payments(); st.llm_usage("2026-11-03"); st.attempt_rows("a0", 0)
            except Exception as e:                                       # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=writer, args=(n,)) for n in range(3)] + [threading.Thread(target=reader) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(st.llm_usage("2026-11-03")["calls"] if isinstance(st.llm_usage("2026-11-03"), dict) else st.llm_usage("2026-11-03")[0], 900)


if __name__ == "__main__":
    unittest.main()

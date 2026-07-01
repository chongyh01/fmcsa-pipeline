"""
orchestrate_reimports.py
Sequential: authority (5 workers) → insurance (5 workers) → revocation (5 workers)
Each stage waits for all workers to finish before starting the next.
"""
import subprocess, concurrent.futures, sys, os, time

CODES = os.path.dirname(os.path.abspath(__file__))

def run_worker(script, worker_id, total):
    print(f"[ORCH] {script} W{worker_id} starting", flush=True)
    r = subprocess.run(
        [sys.executable, "-u", script, "--worker", str(worker_id), "--total", str(total)],
        cwd=CODES,
    )
    print(f"[ORCH] {script} W{worker_id} done (rc={r.returncode})", flush=True)
    return r.returncode

def run_stage(script, total_workers):
    print(f"\n[ORCH] ===== {script} ({total_workers} workers) =====", flush=True)
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=total_workers) as ex:
        futures = {ex.submit(run_worker, script, w, total_workers): w for w in range(total_workers)}
        for f in concurrent.futures.as_completed(futures):
            rc = f.result()
            if rc != 0:
                print(f"[ORCH] WARNING: worker returned rc={rc}", flush=True)
    print(f"[ORCH] ===== {script} DONE in {round((time.time()-t0)/60,1)} min =====\n", flush=True)

if __name__ == "__main__":
    run_stage("reimport_authority_parallel.py", 5)
    run_stage("reimport_insurance_parallel.py", 5)
    run_stage("reimport_revocation_parallel.py", 5)
    print("[ORCH] ALL REIMPORTS COMPLETE.", flush=True)

//! chain-fsck — verify an events.jsonl log's hash chain.
//!
//! Streams lines (multi-GB safe), parses each as JSON, runs `fsck`, prints
//! `CHAIN OK <n> events tip=<hash>` or the first error with line number.

use std::io::{BufRead, BufReader};
use std::process::ExitCode;

use vihs_core::chain::{fsck, ChainError};

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 2 {
        eprintln!("usage: chain-fsck <events.jsonl>");
        return ExitCode::from(2);
    }
    let file = match std::fs::File::open(&args[1]) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("chain-fsck: cannot open {}: {e}", args[1]);
            return ExitCode::from(2);
        }
    };
    let reader = BufReader::new(file);
    let mut values: Vec<serde_json::Value> = Vec::new();
    for (idx, line) in reader.lines().enumerate() {
        let line = match line {
            Ok(l) => l,
            Err(e) => {
                eprintln!("chain-fsck: line {} read error: {e}", idx + 1);
                return ExitCode::FAILURE;
            }
        };
        if line.trim().is_empty() {
            continue;
        }
        match serde_json::from_str::<serde_json::Value>(&line) {
            Ok(v) => values.push(v),
            Err(e) => {
                eprintln!("chain-fsck: line {} parse error: {e}", idx + 1);
                return ExitCode::FAILURE;
            }
        }
    }
    match fsck(values.iter()) {
        Ok((n, tip)) => {
            println!("CHAIN OK {n} events tip={tip}");
            ExitCode::SUCCESS
        }
        Err(ChainError::Torn { at }) => {
            eprintln!("chain-fsck: TORN at event {at} (prev_hash mismatch)");
            ExitCode::FAILURE
        }
        Err(ChainError::BadHash { at }) => {
            eprintln!("chain-fsck: BAD HASH at event {at}");
            ExitCode::FAILURE
        }
        Err(ChainError::TurnRegression { at }) => {
            eprintln!("chain-fsck: TURN REGRESSION at event {at}");
            ExitCode::FAILURE
        }
        Err(e) => {
            eprintln!("chain-fsck: {e}");
            ExitCode::FAILURE
        }
    }
}

//! Rust-owned supervisor for the private Python connector child (P08).
//!
//! Bounded anonymous JSONL pipes only. Framing, validation, and lease
//! construction come from the Codex-owned `crate::authority_auth` helpers;
//! this module only owns the child lifecycle, the monotonic epoch, the
//! bounded relay tasks, and static error codes. Token material, child
//! output, argv, and raw OS error text never enter an error string.

use crate::authority_auth::{
    lease_frame, make_frame, read_frame, validate_authority_frame, validate_connector_frame,
    write_frame,
};
use serde_json::{Value, json};
use std::process::Stdio;
use std::time::Duration;
use tokio::io::AsyncReadExt;
use tokio::process::{Child, ChildStdin};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;

/// Owned handle for exactly one supervised connector child.
///
/// `status` takes `&mut self` because `Child::try_wait` requires a mutable
/// borrow; the fixture holds a `mut` supervisor so every call site compiles.
/// There is intentionally no `Debug` impl: supervisor state must never
/// render into logs or diagnostics.
pub struct ConnectorSupervisor {
    child: Child,
    stdin: Option<ChildStdin>,
    rx: mpsc::Receiver<Result<Option<Value>, String>>,
    stdout_task: JoinHandle<()>,
    stderr_task: JoinHandle<()>,
    program: String,
    args: Vec<String>,
    epoch: u64,
    pid: Option<u32>,
    available: bool,
}

impl ConnectorSupervisor {
    /// Spawn the connector child and complete the `connector_start` handshake.
    pub async fn spawn(program: &str, args: &[String], epoch: u64) -> Result<Self, String> {
        if epoch == 0 {
            return Err("invalid_epoch".to_string());
        }
        let mut child = tokio::process::Command::new(program)
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .map_err(|_| "spawn_failed".to_string())?;
        let pid = child.id();
        let stdin = child.stdin.take().ok_or_else(|| "stdio_missing".to_string())?;
        let stdout = child.stdout.take().ok_or_else(|| "stdio_missing".to_string())?;
        let stderr = child.stderr.take().ok_or_else(|| "stdio_missing".to_string())?;
        let (tx, rx) = mpsc::channel::<Result<Option<Value>, String>>(8);
        let stdout_task = tokio::spawn(async move {
            let mut stdout = stdout;
            loop {
                let result = read_frame(&mut stdout).await;
                let terminal = !matches!(result, Ok(Some(_)));
                if tx.send(result).await.is_err() {
                    break;
                }
                if terminal {
                    break;
                }
            }
        });
        let stderr_task = tokio::spawn(async move {
            let mut stderr = stderr;
            let mut buf = [0u8; 4096];
            loop {
                match stderr.read(&mut buf).await {
                    Ok(0) => break,
                    Ok(_) => continue,
                    Err(_) => break,
                }
            }
        });
        let mut supervisor = Self {
            child,
            stdin: Some(stdin),
            rx,
            stdout_task,
            stderr_task,
            program: program.to_string(),
            args: args.to_vec(),
            epoch,
            pid,
            available: false,
        };
        if let Err(code) = supervisor.handshake().await {
            supervisor.stop().await;
            return Err(code);
        }
        supervisor.available = true;
        Ok(supervisor)
    }

    /// Exchange the `connector_start` bootstrap and require a ready observation.
    async fn handshake(&mut self) -> Result<(), String> {
        let start = make_frame("connector_start", self.epoch, json!({}));
        validate_authority_frame(&start, self.epoch)?;
        let writer = self.stdin.as_mut().ok_or_else(|| "stdio_missing".to_string())?;
        write_frame(writer, &start).await?;
        let received = tokio::time::timeout(Duration::from_secs(5), self.rx.recv())
            .await
            .map_err(|_| "handshake_timeout".to_string())?;
        let response = match received {
            Some(Ok(Some(value))) => value,
            Some(Ok(None)) | Some(Err(_)) | None => {
                return Err("handshake_closed".to_string());
            }
        };
        validate_connector_frame(&response, self.epoch)?;
        let ready = response.get("kind") == Some(&json!("observation"))
            && response.get("body") == Some(&json!({"ready": true}));
        if !ready {
            return Err("handshake_rejected".to_string());
        }
        Ok(())
    }

    /// Report availability, decimal epoch, and the owned child PID.
    ///
    /// Marks the supervisor unavailable when the owned child has exited or
    /// the bounded stdout relay has closed (EOF). Never touches any PID
    /// except the owned child handle.
    pub fn status(&mut self) -> Value {
        match self.child.try_wait() {
            Ok(Some(_)) | Err(_) => {
                self.available = false;
            }
            Ok(None) => {}
        }
        if self.rx.is_closed() {
            self.available = false;
        }
        json!({
            "available": self.available,
            "authority_epoch": self.epoch.to_string(),
            "pid": self.pid,
        })
    }

    /// Validate, write one authority frame, and return the validated reply.
    ///
    /// Validation runs before the pipe write and before the reply is
    /// returned. Any EOF, timeout, validation failure, or I/O error marks
    /// the supervisor unavailable, closes the owned stdin, and returns a
    /// static code.
    pub async fn exchange(&mut self, frame: Value) -> Result<Value, String> {
        if !self.available {
            return Err("connector_unavailable".to_string());
        }
        match self.child.try_wait() {
            Ok(Some(_)) | Err(_) => {
                self.available = false;
                return Err("connector_unavailable".to_string());
            }
            Ok(None) => {}
        }
        validate_authority_frame(&frame, self.epoch)?;
        let writer = self
            .stdin
            .as_mut()
            .ok_or_else(|| "connector_unavailable".to_string())?;
        if write_frame(writer, &frame).await.is_err() {
            self.available = false;
            self.stdin.take();
            return Err("exchange_failed".to_string());
        }
        let received = match tokio::time::timeout(Duration::from_secs(5), self.rx.recv()).await {
            Ok(value) => value,
            Err(_) => {
                self.available = false;
                self.stdin.take();
                return Err("exchange_timeout".to_string());
            }
        };
        let response = match received {
            Some(Ok(Some(value))) => value,
            Some(Ok(None)) | Some(Err(_)) | None => {
                self.available = false;
                self.stdin.take();
                return Err("exchange_failed".to_string());
            }
        };
        match validate_connector_frame(&response, self.epoch) {
            Ok(()) => Ok(response),
            Err(code) => {
                self.available = false;
                self.stdin.take();
                Err(code)
            }
        }
    }

    /// Deliver a private in-memory token lease and require an accept reply.
    ///
    /// `lease_frame` rejects any `sink != "private"` before encoding or
    /// writing; the reply carries only `{"accepted": bool}`, never a token.
    pub async fn lease(&mut self, token: &str, ttl_ms: u64, sink: &str) -> Result<Value, String> {
        let frame = lease_frame(self.epoch, token, ttl_ms, sink)?;
        let response = self.exchange(frame).await?;
        let accepted = response.get("kind") == Some(&json!("effect_result"))
            && response
                .get("body")
                .and_then(|body| body.get("accepted"))
                .and_then(Value::as_bool)
                == Some(true);
        if !accepted {
            self.available = false;
            self.stdin.take();
            return Err("lease_rejected".to_string());
        }
        Ok(response)
    }

    /// Stop the owned child and respawn it under the next monotonic epoch.
    ///
    /// The next epoch is always `epoch + 1` (checked); the old epoch is
    /// never reused and no caller-supplied epoch is accepted. The next
    /// epoch is reserved on `self` before the child launch so a failed
    /// restart still consumes it (1 -> failed 2 -> recovered 3).
    pub async fn restart(&mut self) -> Result<(), String> {
        self.stop().await;
        let next = self
            .epoch
            .checked_add(1)
            .ok_or_else(|| "epoch_overflow".to_string())?;
        self.epoch = next;
        let fresh = Self::spawn(&self.program, &self.args, next).await?;
        *self = fresh;
        Ok(())
    }

    /// Mark unavailable, close stdin, kill/wait only the owned child, and
    /// abort the stdout relay and stderr drain tasks.
    pub async fn stop(&mut self) {
        self.available = false;
        self.stdin.take();
        if can_signal(self.child.id(),self.pid,std::process::id()) {
            let _ = self.child.kill().await;
        }
        self.stdout_task.abort();
        self.stderr_task.abort();
    }
}

fn can_signal(pid:Option<u32>,known:Option<u32>,own:u32)->bool {
    pid.is_some_and(|value|value>1&&value!=own&&Some(value)==known)
}

#[cfg(test)]mod signal_guard_tests {
    use super::can_signal;
    #[test]fn never_signal_a_sentinel_self_or_unknown_target(){
        for (pid,known) in [(None,None),(Some(0),Some(0)),(Some(1),Some(1)),(Some(77),Some(77)),(Some(88),Some(89)),(Some(88),None)] {
            assert!(!can_signal(pid,known,77));
        }
        assert!(can_signal(Some(88),Some(88),77));
    }
}

impl Drop for ConnectorSupervisor {
    fn drop(&mut self) {
        self.stdout_task.abort();
        self.stderr_task.abort();
    }
}

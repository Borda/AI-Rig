/** Windows gate timeouts terminate descendants through the native process-tree command. */
import { spawn } from "node:child_process"
import { EventEmitter } from "node:events"
import { PassThrough } from "node:stream"
import { afterEach, describe, expect, it, vi } from "vitest"
import { runGate } from "../../../src/gates.ts"
import { Project } from "../../helpers/harness.ts"

vi.mock("node:child_process", () => ({ spawn: vi.fn() }))
const platform = Object.getOwnPropertyDescriptor(process, "platform")!

afterEach(() => {
  Object.defineProperty(process, "platform", platform)
  vi.mocked(spawn).mockReset()
  vi.useRealTimers()
})

describe("Windows gate timeout", () => {
  it("falls back to killing the child when taskkill exits nonzero", async () => {
    const project = new Project()
    vi.useFakeTimers()
    const gate = Object.assign(new EventEmitter(), {
      pid: 1234,
      stdout: new PassThrough(),
      stderr: new PassThrough(),
      kill: vi.fn(() => {
        queueMicrotask(() => gate.emit("close", null, "SIGTERM"))
        return true
      }),
    })
    vi.mocked(spawn).mockImplementation((command) => {
      const killer = new EventEmitter()
      if (command === "taskkill") queueMicrotask(() => killer.emit("close", 1, null))
      return (command === "taskkill" ? killer : gate) as ReturnType<typeof spawn>
    })
    Object.defineProperty(process, "platform", { value: "win32" })
    let receipt: Awaited<ReturnType<typeof runGate>> | undefined
    void runGate({ id: "test", command: "sleep", source: "project", needsFiles: false }, project.root, [], 1_000).then(
      (result) => {
        receipt = result
      },
    )
    await vi.advanceTimersByTimeAsync(1_001)
    const settled = receipt
    gate.emit("close", null, "SIGKILL")
    expect(gate.kill).toHaveBeenCalledWith("SIGTERM")
    expect(settled?.receipt.reason).toBe("timeout")
  })

  it("settles at the hard cutoff even if no termination event arrives", async () => {
    const project = new Project()
    vi.useFakeTimers()
    const gate = Object.assign(new EventEmitter(), {
      pid: 1234,
      stdout: new PassThrough(),
      stderr: new PassThrough(),
      kill: vi.fn(() => false),
      unref: vi.fn(),
    })
    vi.mocked(spawn).mockImplementation((command) => {
      const killer = new EventEmitter()
      if (command === "taskkill") queueMicrotask(() => killer.emit("close", 1, null))
      return (command === "taskkill" ? killer : gate) as ReturnType<typeof spawn>
    })
    Object.defineProperty(process, "platform", { value: "win32" })
    let receipt: Awaited<ReturnType<typeof runGate>> | undefined
    void runGate({ id: "test", command: "sleep", source: "project", needsFiles: false }, project.root, [], 1_000).then(
      (result) => {
        receipt = result
      },
    )
    await vi.advanceTimersByTimeAsync(2_001)
    const settled = receipt
    gate.emit("close", null, "SIGKILL")
    expect(settled?.receipt.reason).toBe("timeout")
    expect(settled?.receipt.status).toBe("failed")
    expect(settled?.receipt.tail).toMatch(/termination.*not.*confirmed/)
    expect(gate.unref).toHaveBeenCalledOnce()
  })

  it("kills the complete process tree instead of only the shell", async () => {
    const project = new Project()
    const gate = Object.assign(new EventEmitter(), {
      pid: 1234,
      stdout: new PassThrough(),
      stderr: new PassThrough(),
      kill: vi.fn(() => {
        queueMicrotask(() => gate.emit("close", null, "SIGTERM"))
        return true
      }),
    })
    vi.mocked(spawn).mockImplementation((command) => {
      if (command === "taskkill") queueMicrotask(() => gate.emit("close", null, "SIGTERM"))
      return (command === "taskkill" ? new EventEmitter() : gate) as ReturnType<typeof spawn>
    })
    Object.defineProperty(process, "platform", { value: "win32" })
    const result = await runGate(
      { id: "test", command: "sleep", source: "project", needsFiles: false },
      project.root,
      [],
      1_000,
    )
    expect(spawn).toHaveBeenCalledWith(
      "taskkill",
      ["/PID", "1234", "/T", "/F"],
      expect.objectContaining({ windowsHide: true, stdio: "ignore" }),
    )
    expect(gate.kill).not.toHaveBeenCalled()
    expect(result.receipt.reason).toBe("timeout")
  })
})

// SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
//
// SPDX-License-Identifier: EUPL-1.2

package twotier

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"go/types"
	"os"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// TestSetL1IfCurrentCallsAreBounded catches test calls that can stall under mutation.
func TestSetL1IfCurrentCallsAreBounded(t *testing.T) {
	t.Parallel()
	for _, source := range readBoundedWaitTestSources(t) {
		positions, err := unboundedFillCalls(source.name, source.data)
		require.NoError(t, err)
		for _, position := range positions {
			t.Errorf("cache/twotier: %s: setL1IfCurrent must be called directly inside a startL2LockOperation worker", position)
		}
	}
}

type boundedWaitTestSource struct {
	name string
	data []byte
}

// readBoundedWaitTestSources includes test files behind build tags in the source checks.
func readBoundedWaitTestSources(t *testing.T) []boundedWaitTestSource {
	t.Helper()
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatalf("cache/twotier: list test sources: %v", err)
	}
	sources := make([]boundedWaitTestSource, 0, len(entries))
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, "_test.go") {
			continue
		}
		data, readErr := os.ReadFile(name)
		if readErr != nil {
			t.Fatalf("cache/twotier: read test source %s: %v", name, readErr)
		}
		sources = append(sources, boundedWaitTestSource{name: name, data: data})
	}
	return sources
}

// TestL2LockTestsArmWatchdog catches lock regression tests without a synchronous-call deadline.
func TestL2LockTestsArmWatchdog(t *testing.T) {
	t.Parallel()
	findings, err := missingL2LockWatchdogs(readBoundedWaitTestSources(t))
	require.NoError(t, err)
	for _, finding := range findings {
		t.Errorf("cache/twotier: %s: %s must start with armL2LockWatchdog after optional t.Parallel()", finding.position, finding.name)
	}
}

// TestL2StubHooksOnlyCallAllowedFunctions keeps test assertions off the stub's worker goroutine.
func TestL2StubHooksOnlyCallAllowedFunctions(t *testing.T) {
	t.Parallel()
	for _, source := range readBoundedWaitTestSources(t) {
		calls, err := disallowedL2HookCalls(source.name, source.data)
		require.NoError(t, err)
		for _, call := range calls {
			t.Errorf("cache/twotier: %s: L2 stub hook call to %s is outside the allow-list", call.position, call.callee)
		}
	}
}

type l2HookCall struct {
	position token.Position
	callee   string
}

// disallowedL2HookCalls checks every matching hook body, including its nested callbacks.
func disallowedL2HookCalls(filename string, src []byte) ([]l2HookCall, error) {
	files := token.NewFileSet()
	file, err := parser.ParseFile(files, filename, src, parser.SkipObjectResolution)
	if err != nil {
		return nil, fmt.Errorf("cache/twotier: parse test source %s: %w", filename, err)
	}
	var calls []l2HookCall
	ast.Inspect(file, func(node ast.Node) bool {
		hook, ok := node.(*ast.FuncLit)
		if !ok || !isL2StubHookType(hook.Type) {
			return true
		}
		ast.Inspect(hook.Body, func(child ast.Node) bool {
			if call, called := child.(*ast.CallExpr); called {
				callee := types.ExprString(ast.Unparen(call.Fun))
				if !isAllowedL2HookCall(callee) {
					calls = append(calls, l2HookCall{position: files.PositionFor(call.Pos(), false), callee: callee})
				}
			}
			return true
		})
		return false
	})
	return calls, nil
}

// isL2StubHookType matches the stub signature with grouped, separate, or unnamed parameters.
func isL2StubHookType(signature *ast.FuncType) bool {
	var parameters [3]ast.Expr
	if signature.Params.NumFields() != len(parameters) || signature.Results.NumFields() != 0 {
		return false
	}
	index := 0
	for _, field := range signature.Params.List {
		for range max(1, len(field.Names)) {
			parameters[index] = ast.Unparen(field.Type)
			index++
		}
	}
	value, isArrayType := parameters[2].(*ast.ArrayType)
	return types.ExprString(parameters[0]) == "string" && types.ExprString(parameters[1]) == "string" &&
		isArrayType && value.Len == nil && types.ExprString(ast.Unparen(value.Elt)) == "byte"
}

// isAllowedL2HookCall limits the hook to the calls the current stubs need.
func isAllowedL2HookCall(callee string) bool {
	switch callee {
	case "tryStaleL1Install", "panicOnce.Do", "panic", "string", "signal", "fillHook":
		return true
	default:
		return false
	}
}

type l2LockWatchdogFinding struct {
	position token.Position
	name     string
}

// missingL2LockWatchdogs reports tests that reach a lock worker without arming a watchdog.
func missingL2LockWatchdogs(sources []boundedWaitTestSource) ([]l2LockWatchdogFinding, error) {
	files := token.NewFileSet()
	decls, err := l2LockTestDecls(files, sources)
	if err != nil {
		return nil, err
	}
	helpers := l2LockHelpers(decls)
	var findings []l2LockWatchdogFinding
	for _, decl := range decls {
		if strings.HasPrefix(decl.Name.Name, "Test") && callsL2LockHelper(decl.Body, helpers) && !startsWithL2LockWatchdog(decl.Body.List) {
			findings = append(findings, l2LockWatchdogFinding{position: files.PositionFor(decl.Pos(), false), name: decl.Name.Name})
		}
	}
	return findings, nil
}

// l2LockTestDecls gathers function bodies before resolving calls across test files.
func l2LockTestDecls(files *token.FileSet, sources []boundedWaitTestSource) ([]*ast.FuncDecl, error) {
	var decls []*ast.FuncDecl
	for _, source := range sources {
		file, err := parser.ParseFile(files, source.name, source.data, parser.SkipObjectResolution)
		if err != nil {
			return nil, fmt.Errorf("cache/twotier: parse test source %s: %w", source.name, err)
		}
		for _, decl := range file.Decls {
			if function, ok := decl.(*ast.FuncDecl); ok && function.Body != nil {
				decls = append(decls, function)
			}
		}
	}
	return decls, nil
}

// l2LockHelpers propagates worker reachability until a pass adds no helpers.
func l2LockHelpers(decls []*ast.FuncDecl) map[string]bool {
	helpers := map[string]bool{"startL2LockOperation": true}
	for range decls {
		added := false
		for _, decl := range decls {
			name := decl.Name.Name
			if helpers[name] || strings.HasPrefix(name, "Test") || !callsL2LockHelper(decl.Body, helpers) {
				continue
			}
			helpers[name] = true
			added = true
		}
		if !added {
			break
		}
	}
	return helpers
}

// callsL2LockHelper checks calls inside a function body, including its subtest literals.
func callsL2LockHelper(body *ast.BlockStmt, helpers map[string]bool) bool {
	found := false
	ast.Inspect(body, func(node ast.Node) bool {
		if call, ok := node.(*ast.CallExpr); ok {
			switch callee := ast.Unparen(call.Fun).(type) {
			case *ast.Ident:
				found = found || helpers[callee.Name]
			case *ast.SelectorExpr:
				found = found || helpers[callee.Sel.Name]
			}
		}
		return !found
	})
	return found
}

// startsWithL2LockWatchdog accepts a watchdog call before setup starts.
func startsWithL2LockWatchdog(statements []ast.Stmt) bool {
	if len(statements) > 0 && isL2LockParallelCall(statements[0]) {
		statements = statements[1:]
	}
	if len(statements) == 0 {
		return false
	}
	callee := statementCallee(statements[0])
	name, ok := callee.(*ast.Ident)
	return ok && name.Name == "armL2LockWatchdog"
}

// isL2LockParallelCall recognizes the optional leading t.Parallel statement.
func isL2LockParallelCall(statement ast.Stmt) bool {
	selector, ok := statementCallee(statement).(*ast.SelectorExpr)
	if !ok || selector.Sel.Name != "Parallel" {
		return false
	}
	receiver, named := ast.Unparen(selector.X).(*ast.Ident)
	return named && receiver.Name == "t"
}

// statementCallee extracts the callee of a standalone call statement.
func statementCallee(statement ast.Stmt) ast.Expr {
	expression, ok := statement.(*ast.ExprStmt)
	if !ok {
		return nil
	}
	call, called := ast.Unparen(expression.X).(*ast.CallExpr)
	if !called {
		return nil
	}
	return ast.Unparen(call.Fun)
}

// unboundedFillCalls finds fill references without a direct call inside a bounded worker.
func unboundedFillCalls(filename string, src []byte) ([]token.Position, error) {
	files := token.NewFileSet()
	file, err := parser.ParseFile(files, filename, src, parser.SkipObjectResolution)
	if err != nil {
		return nil, fmt.Errorf("cache/twotier: parse test source %s: %w", filename, err)
	}
	var positions []token.Position
	ast.PreorderStack(file, nil, func(node ast.Node, ancestors []ast.Node) bool {
		selector, ok := node.(*ast.SelectorExpr)
		if ok && selector.Sel.Name == "setL1IfCurrent" && !boundedFillSelector(selector, ancestors) {
			positions = append(positions, files.PositionFor(selector.Pos(), false))
		}
		return true
	})
	return positions, nil
}

// boundedFillSelector requires a direct call to the selector inside the worker.
func boundedFillSelector(selector *ast.SelectorExpr, ancestors []ast.Node) bool {
	for i, ancestor := range slices.Backward(ancestors) {
		if _, parens := ancestor.(*ast.ParenExpr); parens {
			continue
		}
		if !isDirectFillCall(selector, ancestor) {
			return false
		}
		return insideL2LockWorker(ancestors[:i])
	}
	return false
}

// isDirectFillCall excludes method values and selectors passed as arguments.
func isDirectFillCall(selector *ast.SelectorExpr, parent ast.Node) bool {
	call, ok := parent.(*ast.CallExpr)
	return ok && ast.Unparen(call.Fun) == selector
}

// insideL2LockWorker accepts synchronous calls in the nearest worker's run literal.
func insideL2LockWorker(ancestors []ast.Node) bool {
	for i, ancestor := range slices.Backward(ancestors) {
		switch node := ancestor.(type) {
		case *ast.ExprStmt, *ast.BlockStmt, *ast.CommClause, *ast.SelectStmt:
		case *ast.FuncLit:
			return isL2LockWorkerRun(node, ancestors[:i])
		default:
			return false
		}
	}
	return false
}

// isL2LockWorkerRun requires an inline run argument and a synchronous worker start.
func isL2LockWorkerRun(run *ast.FuncLit, ancestors []ast.Node) bool {
	const runArgIndex = 2
	for i := len(ancestors) - 1; i > 0; i-- {
		if _, parens := ancestors[i].(*ast.ParenExpr); parens {
			continue
		}
		call, ok := ancestors[i].(*ast.CallExpr)
		if !ok || len(call.Args) <= runArgIndex || ast.Unparen(call.Args[runArgIndex]) != run {
			return false
		}
		callee, named := ast.Unparen(call.Fun).(*ast.Ident)
		return named && callee.Name == "startL2LockOperation" && isSynchronousWorkerStart(ancestors[:i])
	}
	return false
}

// isSynchronousWorkerStart follows only the statement and subtest forms that own bounded workers.
func isSynchronousWorkerStart(ancestors []ast.Node) bool {
	for i := len(ancestors) - 1; i >= 0; i-- {
		switch node := ancestors[i].(type) {
		case *ast.BlockStmt, *ast.ExprStmt, *ast.AssignStmt, *ast.ReturnStmt, *ast.IfStmt, *ast.ForStmt,
			*ast.RangeStmt, *ast.CaseClause, *ast.SwitchStmt:
		case *ast.FuncDecl:
			return i+1 < len(ancestors) && ancestors[i+1] == node.Body
		case *ast.FuncLit:
			if i < 2 || !isSubtestRunLiteral(node, ancestors[i-1], ancestors[i-2]) {
				return false
			}
			i--
		default:
			return false
		}
	}
	return false
}

// isSubtestRunLiteral recognizes an inline subtest in a direct ident.Run call statement.
func isSubtestRunLiteral(run *ast.FuncLit, parent, grandparent ast.Node) bool {
	call, ok := parent.(*ast.CallExpr)
	if !ok || len(call.Args) != 2 || call.Args[1] != run {
		return false
	}
	statement, standalone := grandparent.(*ast.ExprStmt)
	if !standalone || statement.X != call {
		return false
	}
	selector, selected := call.Fun.(*ast.SelectorExpr)
	if !selected || selector.Sel.Name != "Run" {
		return false
	}
	_, named := selector.X.(*ast.Ident)
	return named
}

// TestSetL1IfCurrentCallsAreBoundedFixtures accepts worker calls and flags escaping calls.
func TestSetL1IfCurrentCallsAreBoundedFixtures(t *testing.T) {
	t.Parallel()
	const planted = `package twotier
func test() {
	stub.beforeWrite = func() {
		view.setL1IfCurrent("n:a", 1, epoch)
	}
}
`
	const release = `package twotier
func test() {
	startL2LockOperation(t, func() {
		view.setL1IfCurrent("n:a", 1, epoch)
	}, run)
}
`
	const nestedRelease = `package twotier
func test() {
	startL2LockOperation(t, nil, func() error {
		startL2LockOperation(t, func() {
			view.setL1IfCurrent("n:a", 1, epoch)
		}, run)
		return nil
	})
}
`
	const nestedCallback = `package twotier
func test() {
	startL2LockOperation(t, nil, func() error {
		t.Cleanup(func() {
			view.setL1IfCurrent("n:a", 1, epoch)
		})
		return nil
	})
}
`
	const goStatement = `package twotier
func test() {
	startL2LockOperation(t, nil, func() error {
		go view.setL1IfCurrent("n:a", 1, epoch)
		return nil
	})
}
`
	requireBoundedFillCallFixture(t)
	requireUnboundedFillCall(t, "planted_test.go", planted, 4)
	requireUnboundedFillCall(t, "release_test.go", release, 4)
	requireUnboundedFillCall(t, "nested_release_test.go", nestedRelease, 5)
	requireUnboundedFillCall(t, "nested_callback_test.go", nestedCallback, 5)
	requireUnboundedFillCall(t, "go_statement_test.go", goStatement, 4)
}

// TestSetL1IfCurrentCallsAreBoundedWorkerFixtures catches detached starts and method values.
func TestSetL1IfCurrentCallsAreBoundedWorkerFixtures(t *testing.T) {
	t.Parallel()
	const goStart = `package twotier
func test() {
	go startL2LockOperation(t, nil, func() error {
		view.setL1IfCurrent("n:a", 1, epoch)
		return nil
	})
}
`
	const deferStart = `package twotier
func test() {
	defer startL2LockOperation(t, nil, func() error {
		view.setL1IfCurrent("n:a", 1, epoch)
		return nil
	})
}
`
	const methodValue = `package twotier
func test() {
	startL2LockOperation(t, nil, func() error {
		install := view.setL1IfCurrent
		install("n:a", 1, epoch)
		return nil
	})
}
`
	requireUnboundedFillCall(t, "go_start_test.go", goStart, 4)
	requireUnboundedFillCall(t, "defer_start_test.go", deferStart, 4)
	requireUnboundedFillCall(t, "method_value_test.go", methodValue, 4)
}

// TestSetL1IfCurrentCallsAreBoundedWrappedWorkerFixtures rejects starts hidden in detached closures.
func TestSetL1IfCurrentCallsAreBoundedWrappedWorkerFixtures(t *testing.T) {
	t.Parallel()
	const goWrapper = `package twotier
func test() {
	go func() {
		startL2LockOperation(t, release, func() error {
			view.setL1IfCurrent("n:a", 1, epoch)
			return nil
		})
	}()
}
`
	const deferWrapper = `package twotier
func test() {
	defer func() {
		startL2LockOperation(t, release, func() error {
			view.setL1IfCurrent("n:a", 1, epoch)
			return nil
		})
	}()
}
`
	const closureWrapper = `package twotier
func test() {
	start := func() {
		startL2LockOperation(t, release, func() error {
			view.setL1IfCurrent("n:a", 1, epoch)
			return nil
		})
	}
	start()
}
`
	cases := [...]struct{ name, source string }{
		{"goWrapper", goWrapper},
		{"deferWrapper", deferWrapper},
		{"closureWrapper", closureWrapper},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(subtest *testing.T) {
			subtest.Parallel()
			requireUnboundedFillCall(subtest, tc.name+"_test.go", tc.source, 5)
		})
	}
}

// requireBoundedFillCallFixture checks that the worker's synchronous run call is accepted.
func requireBoundedFillCallFixture(t *testing.T) {
	t.Helper()
	const subtest = `package twotier
func test() {
	for _, tc := range cases {
		t.Run(tc.name, func(row *testing.T) {
			startL2LockOperation(row, nil, func() error {
				view.setL1IfCurrent("n:a", 1, epoch)
				return nil
			})
		})
	}
}
`
	const selectCase = `package twotier
func test() {
	install := startL2LockOperation(t, nil, func() error {
		select {
		case <-requested:
			(view.setL1IfCurrent)("n:a", 1, epoch)
		case <-ctx.Done():
		}
		return nil
	})
}
`
	for _, source := range [...]string{subtest, selectCase} {
		positions, err := unboundedFillCalls("clean_test.go", []byte(source))
		require.NoError(t, err)
		require.Empty(t, positions)
	}
}

// requireUnboundedFillCall checks that the fixture reports only the planted call.
func requireUnboundedFillCall(t *testing.T, filename, src string, line int) {
	t.Helper()
	positions, err := unboundedFillCalls(filename, []byte(src))
	require.NoError(t, err)
	require.Len(t, positions, 1, filename)
	require.Equal(t, filename, positions[0].Filename)
	require.Equal(t, line, positions[0].Line)
}

// TestL2LockTestsArmWatchdogFixtures covers direct and transitive calls across test files.
func TestL2LockTestsArmWatchdogFixtures(t *testing.T) {
	t.Parallel()
	const planted = `package twotier
func TestPlanted(t *testing.T) {
	startL2LockOperation(t, nil, run)
}
`
	const helperPlanted = `package twotier
func TestHelperPlanted(t *testing.T) {
	helper(t)
}
func helper(t *testing.T) {
	workerHelper(t)
}
`
	const clean = `package twotier
func TestClean(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	startL2LockOperation(t, nil, run)
}
func workerHelper(t *testing.T) {
	startL2LockOperation(t, nil, run)
}
`
	findings, err := missingL2LockWatchdogs([]boundedWaitTestSource{
		{name: "planted_test.go", data: []byte(planted)},
		{name: "helper_planted_test.go", data: []byte(helperPlanted)},
		{name: "clean_test.go", data: []byte(clean)},
	})
	require.NoError(t, err)
	require.Len(t, findings, 2)
	require.Equal(t, "planted_test.go:2:1", findings[0].position.String())
	require.Equal(t, "TestPlanted", findings[0].name)
	require.Equal(t, "helper_planted_test.go:2:1", findings[1].position.String())
	require.Equal(t, "TestHelperPlanted", findings[1].name)
}

// TestL2StubHooksOnlyCallAllowedFunctionsFixtures plants direct and nested test failures.
func TestL2StubHooksOnlyCallAllowedFunctionsFixtures(t *testing.T) {
	t.Parallel()
	const source = `package twotier
func test() {
	stub.beforeWrite = func(op, key string, _ []byte) {
		waitL2LockSignal(t, done, "worker stalled")
	}
	stub.afterWrite = func(string, string, []byte) {
		panicOnce.Do(func() { t.Fatal("worker failed") })
	}
	hook := func(op string, key string, _ []byte) {
		signal()
		<-blocked
	}
}
`
	calls, err := disallowedL2HookCalls("fixture_test.go", []byte(source))
	require.NoError(t, err)
	require.Len(t, calls, 2)
	require.Equal(t, "fixture_test.go", calls[0].position.Filename)
	require.Equal(t, 4, calls[0].position.Line)
	require.Equal(t, "waitL2LockSignal", calls[0].callee)
	require.Equal(t, "fixture_test.go", calls[1].position.Filename)
	require.Equal(t, 7, calls[1].position.Line)
	require.Equal(t, "t.Fatal", calls[1].callee)
}

// TestL2LockWatchdogExpires verifies that expiry names the blocked test and includes its stacks.
func TestL2LockWatchdogExpires(t *testing.T) {
	t.Parallel()
	t.Run("blocked", func(subtest *testing.T) {
		subtest.Parallel()
		expired := make(chan string, 1)
		armL2LockWatchdogFor(subtest, 10*time.Millisecond, func(message string) { expired <- message })
		select {
		case message := <-expired:
			require.Contains(subtest, message, subtest.Name())
			require.Contains(subtest, message, "still running after")
			require.Contains(subtest, message, "goroutine ")
		case <-time.After(l2LockTimeout):
			subtest.Fatal("cache/twotier: watchdog did not expire")
		}
	})
}

// TestL2LockWatchdogStopsAtCleanup proves a completed subtest cannot trigger expiry.
//
//nolint:tparallel // The parent must observe the subtest after its cleanup finishes.
func TestL2LockWatchdogStopsAtCleanup(t *testing.T) {
	t.Parallel()
	expired := make(chan string, 1)
	t.Run("completed", func(subtest *testing.T) {
		armL2LockWatchdogFor(subtest, time.Second, func(message string) { expired <- message })
	})
	select {
	case message := <-expired:
		t.Fatalf("cache/twotier: watchdog expired after cleanup: %s", message)
	case <-time.After(1500 * time.Millisecond):
	}
}

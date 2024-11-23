import torch
import torch.fx
from torch._dynamo.test_case import TestCase
from torch._dynamo.testing import AotEagerAndRecordGraphs, normalize_gm


def extract_graph(fn, *args, **kwargs):
    backend = AotEagerAndRecordGraphs()
    result = torch.compile(backend=backend)(fn)(*args, **kwargs)
    return result, backend.graphs, backend.fw_graphs


def graph_str(gm):
    return normalize_gm(gm.print_readable(print_output=False))


class GraphRegionTrackerTests(TestCase):
    def run_and_return_graphs(self, fn, *args, **kwargs):
        with torch._dynamo.config.patch("use_graph_deduplication", True):
            return extract_graph(fn, *args, **kwargs)

    def test_single_subgraph(self):
        def inner_fn(x, y):
            x0 = x + 1
            y0 = y + 2
            z = x0.sum() + y0.sum()
            return z

        def fn(x, y):
            o0 = inner_fn(x, y)
            o1 = torch.sin(y)
            o2 = inner_fn(x, o1)
            o3 = inner_fn(x, y)
            o4 = o3 * o3
            return o2 * o4

        x = torch.rand(10, 10, requires_grad=True)
        y = torch.rand(10, 20, requires_grad=True)
        x_clone = x.clone().requires_grad_(True)
        y_clone = y.clone().requires_grad_(True)

        ref_result = fn(x, y)
        result, graphs, fw_graphs = self.run_and_return_graphs(fn, x_clone, y_clone)

        torch.allclose(ref_result, result)
        ref_result.sum().backward()
        result.sum().backward()

        self.assertEqual(len(graphs), 1)
        self.assertEqual(len(fw_graphs), 1)
        self.assertExpectedInline(
            graph_str(graphs[0]),
            """\
class GraphModule(torch.nn.Module):
    def forward(self, L_x_: "f32[10, 10]", L_y_: "f32[10, 20]"):
        subgraph_0 = self.subgraph_0
        l_x_ = L_x_
        l_y_ = L_y_
        invoke_subgraph = torch.ops.higher_order.invoke_subgraph(subgraph_0, 'subgraph_0', (l_y_, l_x_));  invoke_subgraph = None

        o1: "f32[10, 20]" = torch.sin(l_y_)

        invoke_subgraph_1 = torch.ops.higher_order.invoke_subgraph(subgraph_0, 'subgraph_0', (o1, l_x_));  o1 = None
        getitem_1 = invoke_subgraph_1[0];  invoke_subgraph_1 = None
        invoke_subgraph_2 = torch.ops.higher_order.invoke_subgraph(subgraph_0, 'subgraph_0', (l_y_, l_x_));  subgraph_0 = l_y_ = l_x_ = None
        getitem_2 = invoke_subgraph_2[0];  invoke_subgraph_2 = None

        o4: "f32[]" = getitem_2 * getitem_2;  getitem_2 = None

        mul_1: "f32[]" = getitem_1 * o4;  getitem_1 = o4 = None
        return (mul_1,)

    class subgraph_0(torch.nn.Module):
        def forward(self, subgraph_input_l_y_, subgraph_input_l_x_):
            y0: "f32[10, 20]" = subgraph_input_l_y_ + 2;  subgraph_input_l_y_ = None

            x0: "f32[10, 10]" = subgraph_input_l_x_ + 1;  subgraph_input_l_x_ = None

            sum_2: "f32[]" = y0.sum();  y0 = None
            sum_1: "f32[]" = x0.sum();  x0 = None
            z: "f32[]" = sum_1 + sum_2;  sum_1 = sum_2 = None
            return (z,)
""",
        )

        self.assertExpectedInline(
            graph_str(fw_graphs[0]),
            """\
class GraphModule(torch.nn.Module):
    def forward(self, primals_1: "f32[10, 10]", primals_2: "f32[10, 20]"):
        sin: "f32[10, 20]" = torch.ops.aten.sin.default(primals_2)

        repeated_subgraph0_1 = self.repeated_subgraph0
        invoke_subgraph_1 = torch.ops.higher_order.invoke_subgraph(repeated_subgraph0_1, '___forward_subgraph_0', (sin, primals_1));  repeated_subgraph0_1 = None
        getitem_1: "f32[]" = invoke_subgraph_1[0];  invoke_subgraph_1 = None
        repeated_subgraph0_2 = self.repeated_subgraph0
        invoke_subgraph_2 = torch.ops.higher_order.invoke_subgraph(repeated_subgraph0_2, '___forward_subgraph_0', (primals_2, primals_1));  repeated_subgraph0_2 = None
        getitem_2: "f32[]" = invoke_subgraph_2[0];  invoke_subgraph_2 = None

        mul: "f32[]" = torch.ops.aten.mul.Tensor(getitem_2, getitem_2)

        mul_1: "f32[]" = torch.ops.aten.mul.Tensor(getitem_1, mul);  mul = None
        return (mul_1, primals_1, primals_2, sin, getitem_1, getitem_2)

    class repeated_subgraph0(torch.nn.Module):
        def forward(self, arg0_1: "f32[10, 20]", arg1_1: "f32[10, 10]"):
            add: "f32[10, 20]" = torch.ops.aten.add.Tensor(arg0_1, 2);  arg0_1 = None
            add_1: "f32[10, 10]" = torch.ops.aten.add.Tensor(arg1_1, 1);  arg1_1 = None
            sum_1: "f32[]" = torch.ops.aten.sum.default(add);  add = None
            sum_2: "f32[]" = torch.ops.aten.sum.default(add_1);  add_1 = None
            add_2: "f32[]" = torch.ops.aten.add.Tensor(sum_2, sum_1);  sum_2 = sum_1 = None
            return (add_2,)
""",
        )

    def test_single_subgraph2(self):
        def fn(x):
            x0 = x + 2
            o = inner_fn(x0)
            o = torch.cos(o)
            o = inner_fn(o)
            return torch.sin(o)

        def inner_fn(x):
            o = x * 7
            o += 1
            o += 2
            return o

        x = torch.rand(10, 10, requires_grad=True)
        x_clone = x.clone().requires_grad_(True)

        ref_result = fn(x)
        result, graphs, fw_graphs = self.run_and_return_graphs(fn, x_clone)

        torch.allclose(ref_result, result)
        ref_result.sum().backward()
        result.sum().backward()
        self.assertEqual(len(graphs), 1)
        self.assertEqual(len(fw_graphs), 1)
        self.assertExpectedInline(
            graph_str(graphs[0]),
            """\
class GraphModule(torch.nn.Module):
    def forward(self, L_x_: "f32[10, 10]"):
        subgraph_0 = self.subgraph_0
        l_x_ = L_x_

        x0: "f32[10, 10]" = l_x_ + 2;  l_x_ = None

        invoke_subgraph = torch.ops.higher_order.invoke_subgraph(subgraph_0, 'subgraph_0', (x0,));  x0 = None
        getitem = invoke_subgraph[0];  invoke_subgraph = None

        o_3: "f32[10, 10]" = torch.cos(getitem);  getitem = None

        invoke_subgraph_1 = torch.ops.higher_order.invoke_subgraph(subgraph_0, 'subgraph_0', (o_3,));  subgraph_0 = o_3 = None
        getitem_1 = invoke_subgraph_1[0];  invoke_subgraph_1 = None

        sin: "f32[10, 10]" = torch.sin(getitem_1);  getitem_1 = None
        return (sin,)

    class subgraph_0(torch.nn.Module):
        def forward(self, subgraph_input_x0):
            o: "f32[10, 10]" = subgraph_input_x0 * 7;  subgraph_input_x0 = None

            o += 1;  o_1: "f32[10, 10]" = o;  o = None

            o_1 += 2;  o_2: "f32[10, 10]" = o_1;  o_1 = None
            return (o_2,)
""",
        )
        self.assertExpectedInline(
            graph_str(fw_graphs[0]),
            """\
class GraphModule(torch.nn.Module):
    def forward(self, primals_1: "f32[10, 10]"):
        add: "f32[10, 10]" = torch.ops.aten.add.Tensor(primals_1, 2);  primals_1 = None

        repeated_subgraph0 = self.repeated_subgraph0
        invoke_subgraph = torch.ops.higher_order.invoke_subgraph(repeated_subgraph0, '___forward_subgraph_0', (add,));  repeated_subgraph0 = None
        getitem: "f32[10, 10]" = invoke_subgraph[0];  invoke_subgraph = None

        cos: "f32[10, 10]" = torch.ops.aten.cos.default(getitem)

        repeated_subgraph0_1 = self.repeated_subgraph0
        invoke_subgraph_1 = torch.ops.higher_order.invoke_subgraph(repeated_subgraph0_1, '___forward_subgraph_0', (cos,));  repeated_subgraph0_1 = None
        getitem_1: "f32[10, 10]" = invoke_subgraph_1[0];  invoke_subgraph_1 = None

        sin: "f32[10, 10]" = torch.ops.aten.sin.default(getitem_1)
        cos_1: "f32[10, 10]" = torch.ops.aten.cos.default(getitem_1);  getitem_1 = None

        sin_1: "f32[10, 10]" = torch.ops.aten.sin.default(getitem);  getitem = None
        neg: "f32[10, 10]" = torch.ops.aten.neg.default(sin_1);  sin_1 = None
        return (sin, add, cos, cos_1, neg)

    class repeated_subgraph0(torch.nn.Module):
        def forward(self, arg0_1: "f32[10, 10]"):
            mul: "f32[10, 10]" = torch.ops.aten.mul.Tensor(arg0_1, 7);  arg0_1 = None
            add: "f32[10, 10]" = torch.ops.aten.add.Tensor(mul, 1);  mul = None
            add_1: "f32[10, 10]" = torch.ops.aten.add.Tensor(add, 2);  add = None
            return (add_1,)
""",
        )

    def test_multiple_subgraphs(self):
        def inner_fn(x, y):
            x1 = x + 1
            y1 = y + 2
            z = x1.sum() + y1.sum()
            return z

        def inner_fn2(a, b):
            a0 = a + 2
            b0 = b + 3
            c = a0 * b0.cos().sum()
            return c

        def fn(x, y):
            x0 = torch.cos(x)
            y0 = torch.sin(y)
            o1 = inner_fn2(x0, y0)
            o0 = inner_fn(x, y)
            o1 = torch.sin(o0)
            o2 = inner_fn(x, y0)
            o3 = inner_fn2(x0, y0)
            o4 = inner_fn(x, y)
            return o1 * o2 * o3 + o4

        x = torch.rand(10, 10, requires_grad=True)
        y = torch.rand(10, 20, requires_grad=True)
        x_clone = x.clone().requires_grad_(True)
        y_clone = y.clone().requires_grad_(True)

        ref_result = fn(x, y)
        result, graphs, fw_graphs = self.run_and_return_graphs(fn, x_clone, y_clone)

        torch.allclose(ref_result, result)
        ref_result.sum().backward()
        result.sum().backward()
        self.assertEqual(len(graphs), 1)
        self.assertEqual(len(fw_graphs), 1)

        self.assertExpectedInline(
            graph_str(graphs[0]),
            """\
class GraphModule(torch.nn.Module):
    def forward(self, L_x_: "f32[10, 10]", L_y_: "f32[10, 20]"):
        subgraph_1 = self.subgraph_1
        subgraph_0 = self.subgraph_0
        l_x_ = L_x_
        l_y_ = L_y_

        x0: "f32[10, 10]" = torch.cos(l_x_)

        y0: "f32[10, 20]" = torch.sin(l_y_)

        invoke_subgraph_3 = torch.ops.higher_order.invoke_subgraph(subgraph_1, 'subgraph_1', (x0, y0));  invoke_subgraph_3 = None
        invoke_subgraph = torch.ops.higher_order.invoke_subgraph(subgraph_0, 'subgraph_0', (l_x_, l_y_))
        getitem = invoke_subgraph[0];  invoke_subgraph = None

        o1: "f32[]" = torch.sin(getitem);  getitem = None

        invoke_subgraph_1 = torch.ops.higher_order.invoke_subgraph(subgraph_0, 'subgraph_0', (l_x_, y0))
        getitem_1 = invoke_subgraph_1[0];  invoke_subgraph_1 = None
        invoke_subgraph_4 = torch.ops.higher_order.invoke_subgraph(subgraph_1, 'subgraph_1', (x0, y0));  subgraph_1 = x0 = y0 = None
        getitem_4 = invoke_subgraph_4[0];  invoke_subgraph_4 = None
        invoke_subgraph_2 = torch.ops.higher_order.invoke_subgraph(subgraph_0, 'subgraph_0', (l_x_, l_y_));  subgraph_0 = l_x_ = l_y_ = None
        getitem_2 = invoke_subgraph_2[0];  invoke_subgraph_2 = None

        mul_2: "f32[]" = o1 * getitem_1;  o1 = getitem_1 = None
        mul_3: "f32[10, 10]" = mul_2 * getitem_4;  mul_2 = getitem_4 = None
        add_13: "f32[10, 10]" = mul_3 + getitem_2;  mul_3 = getitem_2 = None
        return (add_13,)

    class subgraph_1(torch.nn.Module):
        def forward(self, subgraph_input_x0, subgraph_input_y0):
            b0: "f32[10, 20]" = subgraph_input_y0 + 3;  subgraph_input_y0 = None

            cos_1: "f32[10, 20]" = b0.cos();  b0 = None
            sum_1: "f32[]" = cos_1.sum();  cos_1 = None

            a0: "f32[10, 10]" = subgraph_input_x0 + 2;  subgraph_input_x0 = None

            c: "f32[10, 10]" = a0 * sum_1;  a0 = sum_1 = None
            return (c,)

    class subgraph_0(torch.nn.Module):
        def forward(self, subgraph_input_l_x_, subgraph_input_l_y_):
            y1: "f32[10, 20]" = subgraph_input_l_y_ + 2;  subgraph_input_l_y_ = None

            x1: "f32[10, 10]" = subgraph_input_l_x_ + 1;  subgraph_input_l_x_ = None

            sum_3: "f32[]" = y1.sum();  y1 = None
            sum_2: "f32[]" = x1.sum();  x1 = None
            z: "f32[]" = sum_2 + sum_3;  sum_2 = sum_3 = None
            return (z,)
""",
        )
        self.assertExpectedInline(
            graph_str(fw_graphs[0]),
            """\
class GraphModule(torch.nn.Module):
    def forward(self, primals_1: "f32[10, 10]", primals_2: "f32[10, 20]"):
        cos: "f32[10, 10]" = torch.ops.aten.cos.default(primals_1)

        sin: "f32[10, 20]" = torch.ops.aten.sin.default(primals_2)

        repeated_subgraph1 = self.repeated_subgraph1
        invoke_subgraph_1 = torch.ops.higher_order.invoke_subgraph(repeated_subgraph1, '___forward_subgraph_0', (primals_1, primals_2));  repeated_subgraph1 = None
        getitem_1: "f32[]" = invoke_subgraph_1[0];  invoke_subgraph_1 = None

        sin_1: "f32[]" = torch.ops.aten.sin.default(getitem_1)

        repeated_subgraph1_1 = self.repeated_subgraph1
        invoke_subgraph_2 = torch.ops.higher_order.invoke_subgraph(repeated_subgraph1_1, '___forward_subgraph_0', (primals_1, sin));  repeated_subgraph1_1 = None
        getitem_2: "f32[]" = invoke_subgraph_2[0];  invoke_subgraph_2 = None
        repeated_subgraph0_1 = self.repeated_subgraph0
        invoke_subgraph_3 = torch.ops.higher_order.invoke_subgraph(repeated_subgraph0_1, '___forward_subgraph_1', (cos, sin));  repeated_subgraph0_1 = None
        getitem_3: "f32[10, 10]" = invoke_subgraph_3[0];  invoke_subgraph_3 = None
        repeated_subgraph1_2 = self.repeated_subgraph1
        invoke_subgraph_4 = torch.ops.higher_order.invoke_subgraph(repeated_subgraph1_2, '___forward_subgraph_0', (primals_1, primals_2));  repeated_subgraph1_2 = None
        getitem_4: "f32[]" = invoke_subgraph_4[0];  invoke_subgraph_4 = None

        mul: "f32[]" = torch.ops.aten.mul.Tensor(sin_1, getitem_2);  sin_1 = None
        mul_1: "f32[10, 10]" = torch.ops.aten.mul.Tensor(mul, getitem_3);  mul = None
        add: "f32[10, 10]" = torch.ops.aten.add.Tensor(mul_1, getitem_4);  mul_1 = getitem_4 = None
        return (add, primals_1, primals_2, cos, sin, getitem_1, getitem_2, getitem_3)

    class repeated_subgraph1(torch.nn.Module):
        def forward(self, arg0_1: "f32[10, 10]", arg1_1: "f32[10, 20]"):
            add: "f32[10, 20]" = torch.ops.aten.add.Tensor(arg1_1, 2);  arg1_1 = None
            add_1: "f32[10, 10]" = torch.ops.aten.add.Tensor(arg0_1, 1);  arg0_1 = None
            sum_1: "f32[]" = torch.ops.aten.sum.default(add);  add = None
            sum_2: "f32[]" = torch.ops.aten.sum.default(add_1);  add_1 = None
            add_2: "f32[]" = torch.ops.aten.add.Tensor(sum_2, sum_1);  sum_2 = sum_1 = None
            return (add_2,)

    class repeated_subgraph0(torch.nn.Module):
        def forward(self, arg0_1: "f32[10, 10]", arg1_1: "f32[10, 20]"):
            add: "f32[10, 20]" = torch.ops.aten.add.Tensor(arg1_1, 3);  arg1_1 = None
            cos: "f32[10, 20]" = torch.ops.aten.cos.default(add);  add = None
            sum_1: "f32[]" = torch.ops.aten.sum.default(cos);  cos = None
            add_1: "f32[10, 10]" = torch.ops.aten.add.Tensor(arg0_1, 2);  arg0_1 = None
            mul: "f32[10, 10]" = torch.ops.aten.mul.Tensor(add_1, sum_1);  add_1 = sum_1 = None
            return (mul,)
""",
        )


if __name__ == "__main__":
    from torch._dynamo.test_case import run_tests

    run_tests()

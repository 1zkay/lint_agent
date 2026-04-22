module dead_constant_if (
    input  wire clk,
    input  wire a,
    input  wire b,
    output reg  y
);
localparam USE_A_PATH = 1'b0;

always @(posedge clk) begin
    if (USE_A_PATH) begin
        y <= a;          // unreachable: USE_A_PATH is tied to 0
    end else begin
        y <= b;
    end
end

endmodule

module case_missing_3bit_multiple (
    input  wire [2:0] opcode,
    input  wire [7:0] src0,
    input  wire [7:0] src1,
    output reg  [7:0] result
);

always @(*) begin
    case (opcode)
        3'b000: result = src0;
        3'b001: result = src1;
        3'b100: result = src0 + src1;
        3'b111: result = 8'hff;
    endcase
end

endmodule
